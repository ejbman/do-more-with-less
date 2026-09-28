#!/usr/bin/env python3
"""do-more-with-less — subscription-aware LLM routing proxy (Python reference implementation).

An OpenAI-compatible endpoint that routes each request down a *chain* you define:
your prepaid plans and subscriptions first, metered pay-per-token APIs last.
Hot-reloading config, per-provider circuit breakers, per-consumer chains,
quality gate, streaming support. The whole router is this one file.

Why: OpenRouter's Auto Router picks models by aggregate market spend. This
router picks models by *your* balance sheet. If you already pay for a plan,
that plan should be exhausted before a metered endpoint bills you a cent.

Quick start:
    pip install aiohttp
    cp config/models.json.example config/models.json
    cp config/api_keys.json.example config/api_keys.json   # then edit
    python router.py

    # then point any OpenAI-compatible client at http://localhost:8756/v1

Configuration lives in config/models.json (hot-reloads on mtime change).
Secrets live in config/api_keys.json (never committed).

License: MIT
"""
import aiohttp
from aiohttp import web
import json, time, re, sys, os, threading

BASE = os.environ.get("DMWL_BASE_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config"))
MODELS_PATH = os.path.join(BASE, "models.json")
KEYS_PATH = os.path.join(BASE, "api_keys.json")
LOG_PATH = os.environ.get("DMWL_LOG_PATH", os.path.join(BASE, "decisions.jsonl"))
PORT = int(os.environ.get("DMWL_PORT", "8756"))

# ------------------------------------------------------------ hot reload ----
_mtime = {"models": 0, "keys": 0}
def _load():
    global MODELS, KEYS
    try:
        m = os.path.getmtime(MODELS_PATH)
        if m != _mtime["models"]:
            with open(MODELS_PATH) as f:
                MODELS = json.load(f)
            _mtime["models"] = m
    except Exception:
        if "MODELS" not in globals():
            MODELS = {"chains": {}, "pins": {}, "consumers": {}}
    try:
        k = os.path.getmtime(KEYS_PATH)
        if k != _mtime["keys"]:
            with open(KEYS_PATH) as f:
                KEYS = json.load(f)
            _mtime["keys"] = k
    except Exception:
        if "KEYS" not in globals():
            KEYS = {}

def _validate_startup():
    """Refuse to start with placeholder keys — prevents silent key-leak footguns."""
    _load()
    for name, key in KEYS.items():
        if isinstance(key, str) and any(w in key.upper() for w in ("REPLACE", "EXAMPLE", "PASTE-")):
            print(f"ERROR: api_keys.json still contains placeholder value for {name!r}.", file=sys.stderr)
            print("Edit config/api_keys.json before starting the router.", file=sys.stderr)
            sys.exit(1)

_load()
_reloader = threading.Thread(target=lambda: [(_load(), time.sleep(2)) for _ in iter(int, 1)], daemon=True)
_reloader.start()

# --------------------------------------------------------------- breaker ----
BREAKER = {}
BR = MODELS.get("breaker", {"rate_limit_ttl": 600, "other_ttl": 120, "max_backoff": 1800})

def host_of(t):
    m = re.match(r"https?://([^/]+)", t.get("base_url", ""))
    return m.group(1) if m else "?"

def breaker_open(t):
    st = BREAKER.get((host_of(t), t["model"]))
    return bool(st and time.time() < st["until"])

def breaker_trip(t, err):
    """A provider failing should cost it future attempts: exponential-ish backoff.
    Rate-limit-class failures (429/402/403) cool down longer than blips."""
    m = re.search(r"HTTP (\d{3})", err or "")
    status = int(m.group(1)) if m else 0
    ttl = BR["rate_limit_ttl"] if status in (429, 402, 403) else BR["other_ttl"]
    key = (host_of(t), t["model"])
    prev = BREAKER.get(key, {"trips": 0})
    BREAKER[key] = {"until": time.time() + min(ttl * (prev["trips"] + 1), BR["max_backoff"]),
                    "trips": prev["trips"] + 1, "reason": err[:120]}

def breaker_clear(t):
    BREAKER.pop((host_of(t), t["model"]), None)

# ---------------------------------------------------------------- routing ---
def resolve_chain(entry_ref):
    """entry_ref: chain name or pin name -> ordered upstream list (healthy first)."""
    chains = MODELS.get("chains", {})
    pins = MODELS.get("pins", {})
    name = pins.get(entry_ref, entry_ref)
    chain = chains.get(name, [])
    out = []
    for e in chain:
        e = dict(e)
        key_name = e.pop("key", None)
        e["api_key"] = KEYS.get(key_name, "")
        out.append(e)
    healthy = [t for t in out if not breaker_open(t)]
    return healthy if healthy else out  # all broken? try them anyway

def consumer_policy(request):
    """Per-consumer chains: X-Consumer: aider -> coding chain; kids-bot -> cheap chain."""
    c = (request.headers.get("X-Consumer") or "default").strip().lower()
    pol = MODELS.get("consumers", {}).get(c)
    if not pol:
        pol = MODELS.get("consumers", {}).get("default", {"chain": "default"})
    return c, pol

async def try_upstream(sess, target, payload, consumer):
    fwd = dict(payload)
    fwd["model"] = target["model"]
    # Reasoning-token guard: some providers bill reasoning as output by default.
    # Unless the caller asked for thinking, suppress it on known offenders.
    if not any(k in fwd for k in ("reasoning_effort", "reasoning", "thinking", "enable_thinking", "think")):
        if host_of(target) in MODELS.get("suppress_thinking_hosts", []):
            fwd["reasoning_effort"] = "low"
    url = target["base_url"].rstrip("/") + "/chat/completions"
    hdrs = {"Content-Type": "application/json",
            "Authorization": f"Bearer {target['api_key']}"}
    try:
        resp = await sess.post(url, headers=hdrs, json=fwd)
        if resp.status != 200:
            body = (await resp.text())[:300]
            resp.release()
            err = f"{target['model']}@{host_of(target)} HTTP {resp.status}: {body}"
            breaker_trip(target, err)
            return None, err, None
        breaker_clear(target)
        return resp, None, None
    except Exception as e:
        err = f"{target['model']}@{host_of(target)} network: {e}"
        breaker_trip(target, err)
        return None, err, None

def content_usable(data):
    """Quality gate: empty content counts as failure and burns the next hop."""
    try:
        choices = data.get("choices") or []
        if not choices:
            return False
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            content = " ".join(str(p) for p in content)
        return bool(content and content.strip())
    except Exception:
        return False

def log_decision(session, consumer, chain_name, model_used, hop, status, note=""):
    if not LOG_PATH:
        return
    try:
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                                "session": session, "consumer": consumer,
                                "chain": chain_name, "model": model_used,
                                "hop": hop, "status": status, "note": note}) + "\n")
    except Exception:
        pass

async def handle_chat(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    session_key = request.headers.get("X-Session-Id") or ""
    consumer, policy = consumer_policy(request)
    chain_name = policy.get("chain", "default")
    escalate_on = set(policy.get("escalate_on", ["http_5xx", "network", "auth", "empty_content"]))
    requested = (payload.get("model") or "").strip()
    # A request for a pinned alias (e.g. "router-auto") resolves through pins;
    # a request naming a chain directly uses that chain.
    if requested and requested in MODELS.get("pins", {}):
        chain_name = MODELS["pins"][requested] if MODELS["pins"][requested] in MODELS.get("chains", {}) else requested
    stream = bool(payload.get("stream"))

    chain = resolve_chain(chain_name)
    esc_chain = resolve_chain("escalate") if chain_name != "escalate" else []
    if not chain:
        return web.json_response({"error": f"no upstreams for chain {chain_name!r}"}, status=400)

    timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_read=600)
    errors, hop = [], 0
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        for phase, targets in (("primary", chain), ("escalate", esc_chain)):
            for target in targets:
                hop += 1
                resp, err, _ = await try_upstream(sess, target, payload, consumer)
                if err:
                    errors.append(err)
                    log_decision(session_key, consumer, phase if phase == "escalate" else chain_name, target["model"], hop, "fail", err[:80])
                    continue
                if not stream:
                    data = await resp.json()
                    if "empty_content" in escalate_on and not content_usable(data) and hop < len(chain) + len(esc_chain):
                        errors.append(f"{target['model']}: empty_content")
                        log_decision(session_key, consumer, phase if phase == "escalate" else chain_name, target["model"], hop, "empty")
                        continue
                    data.setdefault("model", target["model"])
                    data.setdefault("router_info", {"consumer": consumer, "chain": chain_name, "hop": hop})
                    log_decision(session_key, consumer, phase if phase == "escalate" else chain_name, target["model"], hop, "ok-escalated" if phase == "escalate" else "ok")
                    return web.json_response(data)
                response = web.StreamResponse(status=200, headers={
                    "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                    "Connection": "keep-alive"})
                await response.prepare(request)
                async for chunk in resp.content.iter_any():
                    await response.write(chunk)
                await response.write_eof()
                log_decision(session_key, consumer, phase if phase == "escalate" else chain_name, target["model"], hop, "ok-stream")
                return response

    return web.json_response({"error": "all upstreams failed (incl. escalation)",
                              "details": errors}, status=502)

async def handle_models(request):
    chains = MODELS.get("chains", {})
    ids = sorted(set(list(MODELS.get("pins", {}).keys()) + list(chains.keys())))
    return web.json_response({"object": "list", "data": [
        {"id": i, "object": "model", "owned_by": "do-more-with-less"} for i in ids]})

async def handle_health(request):
    return web.json_response({
        "ok": True,
        "chains": {k: [f"{u['model']}@{host_of(u)}" for u in v] for k, v in MODELS.get("chains", {}).items()},
        "pins": MODELS.get("pins", {}),
        "consumers": {k: v for k, v in MODELS.get("consumers", {}).items() if not k.startswith("_")},
        "breakers": [{"target": f"{h}:{m}", "cooling": int(st["until"] - time.time()),
                      "trips": st["trips"], "reason": st["reason"]}
                     for (h, m), st in BREAKER.items() if st["until"] > time.time()]})

def build_app():
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_post("/v1/chat/completions", handle_chat)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    return app

if __name__ == "__main__":
    _validate_startup()
    web.run_app(build_app(), host="0.0.0.0", port=PORT, print=None)
