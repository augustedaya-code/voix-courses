FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY voices.json download_voices.py ./
RUN python download_voices.py

COPY . .
RUN mkdir -p /app/cache && chown -R user:user /app
USER user

ENV PORT=10000 \
    TTS_CONCURRENCY=1 \
    CACHE_DIR=/app/cache \
    PYTHONUNBUFFERED=1
EXPOSE 10000
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
