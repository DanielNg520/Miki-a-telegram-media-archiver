FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY miki_sorter_bot ./miki_sorter_bot

RUN pip install --no-cache-dir .

# Process-level backstop: probe the in-process health server and let the
# container runtime restart a wedged process (use with `restart: unless-stopped`).
# No-op unless HEALTH_SERVER_ENABLED is set, so polling deployments are unaffected.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request\nif os.environ.get('HEALTH_SERVER_ENABLED','').lower() not in ('1','true','yes'): sys.exit(0)\nurl='http://127.0.0.1:%s/healthz'%os.environ.get('HEALTH_PORT','8081')\ntry: sys.exit(0 if urllib.request.urlopen(url,timeout=4).status==200 else 1)\nexcept Exception: sys.exit(1)"]

CMD ["miki-sorter"]
