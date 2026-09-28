# Do More with Less

**Stop Paying Twice for AI.**

An OpenAI-compatible LLM router that routes by *your* balance sheet, not the market's.
You already pay for AI subscriptions, prepaid plans, and a GPU in the closet. Don't pay
metered API rates for the same tokens.

```text
┌─────────────┐   ┌──────────────────────────────────────────────────┐
│ Any OpenAI- │   │ do-more-with-less router                          │
│ compatible  ├──►│                                                  │
│ client      │   │  chain: [1] your prepaid plan   (already paid)   │
└─────────────┘   │          [2] ollama.com quota   (already paid)   │
 Aider, chat      │          [3] OpenRouter         (metered, last)  │
 bots, scripts    │          [4] escalate chain     (when all fail)  │
                  └──────────────────────────────────────────────────┘
```

## Why not OpenRouter's Auto Router?

| | OpenRouter Auto | do-more-with-less |
|---|---|---|
| Model pick | Aggregate market spend (7-day window, ~30 task classes) | **Your declared chains** |
| Cost control | Suggest a tier; the crowd decides | Deterministic; sunk-cost-first |
| Subscriptions / prepaid plans | Not modeled | **First-class citizens** |
| Local models (Ollama/vLLM) | No | First-class citizens |
| Failure policy | Fallback inside OpenRouter's catalog | Cross-provider + circuit breakers + backoff |
| Config | Opaque, server-side | One JSON file, hot-reloads in 2s |
| Runs where | Their cloud | **Your hardware, your privacy** |

One-line pitch: **OpenRouter routes by what the crowd pays for. This routes by what YOU already paid for.**

## Quick start (5 minutes)

```bash
git clone https://github.com/ejbman/do-more-with-less
cd do-more-with-less/py
pip install aiohttp

cp ../config/models.json.example ../config/models.json
cp ../config/api_keys.json.example ../config/api_keys.json
$EDITOR ../config/api_keys.json        # paste real keys

DMWL_BASE_DIR=../config python router.py   # serves http://localhost:8756/v1
```

Point any OpenAI-compatible client at it:

```bash
curl http://localhost:8756/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "router-auto", "messages": [{"role": "user", "content": "hi"}]}'
```

Or Docker:

```bash
docker compose up -d
```

## How it works

- **Chains** are ordered upstream lists. First healthy provider wins. Put what you've
  already paid for first; metered APIs last.
- **Pins** are aliases clients can request as the model name (`router-auto`, `router-local`).
- **Consumers** give each client its own chain via the `X-Consumer` header — the kids'
  chatbot gets the cheap chain, your coding agent gets the quality chain, anything
  sensitive gets local-only.
- **Circuit breakers** remember failing providers: rate-limits (429/402/403) cool down
  longer than transient errors, with exponential backoff up to a cap.
- **Quality gate**: an empty response counts as a failure and the request moves to the
  next hop — you never see a blank reply because a model "thought" its whole budget away.
- **Hot reload**: edit `models.json`, save, and the router picks it up in ≤2 seconds.
  No restarts.

The entire Python router is one auditable file: [`py/router.py`](py/router.py) (~270 lines).
A Go single-binary port lives in [`go/`](go/). Both read the same `models.json`.

## Config packs

Drop-in `models.json` variants for common setups:

| Pack | Chain philosophy |
|---|---|
| [Coder Pack](packs/coder.json) | Coding-plan first, escalate to frontier model only on failure |
| [Family Pack](packs/family.json) | Cheap models by default, kids' bot never touches metered APIs |
| [Privacy Pack](packs/privacy.json) | Local-only chain; metered endpoints disabled entirely |
| [Quota Burner Pack](packs/quota-burner.json) | Maximum subscription utilization before any metered call |

## Requirements

- Python 3.10+ and `aiohttp` (or Docker), **or** the Go binary (any platform)
- At least one provider API key — even a free local Ollama counts

## License

MIT — see [LICENSE](LICENSE). Config packs and documentation are part of the paid kit;
the router itself is and always will be free and open.
