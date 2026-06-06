# ──────────────────────────────────────────────────────────────────────────────
# edgar-data-init  —  SEC EDGAR bulk data downloader
# ──────────────────────────────────────────────────────────────────────────────
# Build:  docker build -t edgar-data-init:latest .
# Run:    docker run -v /your/data:/edgar-data edgar-data-init:latest
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.13-slim

# --- Non-root user for security -----------------------------------------------
# The NFS share must allow writes from UID 1000 (appuser).
# On the NFS server: chown 1000:1000 /srv/nfs/edgar-data
RUN useradd --create-home --shell /bin/bash appuser
WORKDIR /app

# --- Python dependencies -------------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && rm requirements.txt

# --- Application source -------------------------------------------------------
COPY setup.py .

USER appuser

# --- Runtime ------------------------------------------------------------------
# --mode full   downloads reference + companyfacts + submissions (~5 GB total)
# --data-dir    must match the volume mountPath in the CronJob
# Stages already marked complete in .state.yaml are skipped automatically.
CMD ["python", "setup.py", "--mode", "full", "--data-dir", "/edgar-data/full"]
