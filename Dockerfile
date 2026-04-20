# Multi-stage build for the Trellis REST API + UI.
#
# Stage 1 (builder): install dependencies into a venv using uv.
# Stage 2 (runtime): copy the venv + source into a slim image.
#
# Build:   docker build -t trellis-ai .
# Run:     docker run --rm -p 8420:8420 \
#            -e TRELLIS_CONFIG_DIR=/etc/trellis \
#            -v $PWD/config:/etc/trellis:ro \
#            trellis-ai

# ---------- builder ----------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN pip install --no-cache-dir uv

WORKDIR /build

# Copy only what hatchling needs to build the wheel.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN uv venv /opt/venv \
 && uv pip install ".[cloud,llm-openai]"

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    TRELLIS_LOG_FORMAT=json \
    TRELLIS_CONFIG_DIR=/etc/trellis

RUN groupadd --system --gid 1000 trellis \
 && useradd --system --uid 1000 --gid trellis --home /home/trellis --create-home trellis

COPY --from=builder /opt/venv /opt/venv

USER trellis
WORKDIR /home/trellis

EXPOSE 8420

# Container-level healthcheck. Orchestrators (ECS, K8s) should use
# /healthz (liveness) and /readyz (readiness) on their own probes;
# this HEALTHCHECK is a fallback for docker-compose and plain docker run.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8420/healthz', timeout=2).status==200 else 1)"

ENTRYPOINT ["trellis", "serve"]
CMD ["--host", "0.0.0.0", "--port", "8420"]
