# ── build: venv in the Wolfi -dev image ──────────────────────────────────────
FROM cgr.void42.internal/chainguard/python:latest-dev AS build
USER root
ENV PIP_INDEX_URL=https://nexus.void42.internal/repository/pypi-proxy/simple/ \
    PIP_TRUSTED_HOST=nexus.void42.internal
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── runtime: distroless Chainguard python; uid 1000 to match NFS ownership ────
FROM cgr.void42.internal/chainguard/python:latest
WORKDIR /app
COPY --from=build /venv /venv
ENV PATH="/venv/bin:$PATH"
COPY setup.py .
USER 1000
ENTRYPOINT ["/venv/bin/python"]
CMD ["setup.py", "--mode", "full", "--data-dir", "/edgar-data/full"]
