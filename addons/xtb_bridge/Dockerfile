FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    "xtb-api-python[totp]==0.10.0" \
    "aiohttp==3.10.11" \
    && python -m playwright install --with-deps chromium

COPY bridge.py /app/bridge.py

CMD ["python", "/app/bridge.py"]
