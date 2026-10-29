# NyaySutra AI — container image (runs on any Docker host: a VM, Oracle/AWS/DigitalOcean, Google Cloud Run…).
# Build on the machine that has the law library (data/library, incl. the India Code acts imported at run time):
#     docker build -t nyaysutra .
# Secrets are NOT in the image: pass them at run time (docker compose env_file: .env, or the host's secret settings).
# See docs/DEPLOYMENT.md.
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    NYAYA_HOME=/var/lib/nyaysutra \
    MODEL_CACHE=/opt/nyaysutra/models \
    HF_HUB_DISABLE_PROGRESS_BARS=1

# libgomp1: OpenMP runtime used by ONNX Runtime (embeddings, re-ranker, OCR).
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/nyaysutra/app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY nyaya ./nyaya
COPY web ./web
COPY data/library ./data/library
COPY data/awareness ./data/awareness
COPY data/drafting ./data/drafting
COPY supabase ./supabase
COPY scripts ./scripts
COPY run.py .

# Download the search models once, at build time, so a new container starts without fetching ~190 MB.
RUN python -c "from nyaya import embeddings; embeddings.warm_up()"

RUN useradd --system --create-home --home-dir /var/lib/nyaysutra nyaya \
    && mkdir -p /var/lib/nyaysutra /opt/nyaysutra/app/data/uploads \
    && chown -R nyaya /var/lib/nyaysutra /opt/nyaysutra/app/data /opt/nyaysutra/models
USER nyaya

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1
CMD ["python", "run.py", "--no-browser"]
