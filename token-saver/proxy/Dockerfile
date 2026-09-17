FROM python:3.11-slim

WORKDIR /app

COPY proxy/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy/ ./proxy/

# pricing.json is THE price source (loaded at startup — config.load_pricing);
# without it the container silently falls back to the hardcoded table and
# every cost_saved number drifts (QA caught the startup warning 2026-09-17).
COPY pricing.json ./pricing.json

EXPOSE 8000

# Pre-download the compression model at build time is skipped on purpose:
# weights are cached in a volume at first run instead (keeps image small).
CMD ["uvicorn", "proxy.main:app", "--host", "0.0.0.0", "--port", "8000"]
