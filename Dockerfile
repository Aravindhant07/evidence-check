# evidence-check: API + reviewer UI, workers, MCP server. CPU only; models run locally and offline after download.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/models TRANSFORMERS_NO_ADVISORY_WARNINGS=1

RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# CPU-only PyTorch first (the default wheel bundles CUDA libraries this image does not need)
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.2"
COPY pyproject.toml README.md ./
COPY evidence_check ./evidence_check
RUN pip install ".[models,mcp]"

RUN useradd --create-home --uid 10001 evcheck && mkdir -p /data /models && chown evcheck:evcheck /data /models
USER evcheck
VOLUME ["/data", "/models"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
CMD ["evcheck", "serve", "--host", "0.0.0.0", "--port", "8000", "--data", "/data", "--workers", "0"]
