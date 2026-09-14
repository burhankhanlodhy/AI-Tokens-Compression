FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Pre-download the compression model at build time is skipped on purpose:
# weights are cached in a volume at first run instead (keeps image small).
CMD ["uvicorn", "proxy.main:app", "--host", "0.0.0.0", "--port", "8000"]
