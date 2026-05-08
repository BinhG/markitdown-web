FROM python:3.12-slim

# System deps: ffmpeg for audio, libmagic for file detection
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY app.py .
COPY static/ static/

# Non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Port (Coolify injects $PORT env, default 5050)
ENV PORT=5050
EXPOSE 5050

# Gunicorn: 4 workers x 2 threads, 120s timeout
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 4 --threads 2 --timeout 120 --access-logfile - --error-logfile - --log-level info app:app"]
