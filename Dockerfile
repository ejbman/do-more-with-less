FROM python:3.12-slim

WORKDIR /app

RUN groupadd -r router && useradd -r -g router router

COPY py/router.py /app/router.py
COPY config/models.json.example /app/config/models.json.example
COPY config/api_keys.json.example /app/config/api_keys.json.example

RUN pip install --no-cache-dir aiohttp && \
    chown -R router:router /app

USER router

ENV DMWL_BASE_DIR=/app/config
ENV DMWL_PORT=8756

EXPOSE 8756

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8756/health')" || exit 1

# Copy examples to real config on first run if missing (compose usually mounts a volume here)
CMD ["sh", "-c", "cp -n /app/config/models.json.example /app/config/models.json 2>/dev/null; cp -n /app/config/api_keys.json.example /app/config/api_keys.json 2>/dev/null; python /app/router.py"]
