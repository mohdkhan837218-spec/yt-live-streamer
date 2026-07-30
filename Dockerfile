# ──────────────────────────────────────────────────────────────────────────────
# YouTube 24/7 Live Streamer — Dockerfile
# Target Platform: Hugging Face Spaces (Docker SDK)
# ──────────────────────────────────────────────────────────────────────────────

FROM python:3.10-slim

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        procps \
        curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ── Working directory ─────────────────────────────────────────────────────────
WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application code ──────────────────────────────────────────────────────────
COPY . .

# ── Create templates directory if it doesn't exist ────────────────────────────
RUN mkdir -p templates

# ── Hugging Face Spaces runs containers as a non-root user (uid=1000) ─────────
RUN useradd -m -u 1000 streamer && chown -R streamer:streamer /app
USER streamer

# ── Expose the required HF Spaces port ───────────────────────────────────────
EXPOSE 7860

# ── Health check ─────────────────────────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:7860/api/status || exit 1

# ── Launch application ────────────────────────────────────────────────────────
CMD ["python", "app.py"]
