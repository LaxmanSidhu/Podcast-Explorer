FROM python:3.12-slim

# FFmpeg (which also provides ffprobe) is required for the audio clip feature.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first so this layer is cached across code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway (and most PaaS hosts) inject the port to listen on via $PORT.
# One worker (with many threads) is required because the bulk-download queue and
# job progress are tracked in this process's memory; multiple workers wouldn't
# share them. Threads handle concurrent requests fine since the work is I/O bound
# (downloading, and streaming finished zips to users). To serve more people, give
# this one instance more CPU/RAM/disk (scale up) rather than adding replicas.
CMD ["sh", "-c", "gunicorn app:app --bind 0.0.0.0:${PORT:-8000} --workers 1 --threads 24 --timeout 600"]
