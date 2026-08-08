# Built for arm64 (Graviton). The dev machine is Apple Silicon, so this is a
# native build both ways — no emulation, no cross-compilation.

FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the lockfile in their own layer, so editing source
# does not re-resolve and re-download the world on every build.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY basivo_orch ./basivo_orch
COPY migrations ./migrations
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12-slim-bookworm AS runtime

# curl is for the container healthcheck; nothing else is added. A slim runtime
# is a smaller attack surface and a faster pull on a 1GB instance.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 basivo

WORKDIR /app
COPY --from=builder --chown=basivo:basivo /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --chown=basivo:basivo docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER basivo
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]

# --no-server-header is not optional. uvicorn appends its own `Server:` header
# *after* the ASGI app returns, so the security middleware cannot remove it;
# this flag is the only way to stop advertising the server and its version.
#
# One worker, deliberately. The box has 1GB and a single run executor owns the
# in-memory event sequence for a run — a second worker would hand out duplicate
# sequence numbers. Scaling out means moving runs to a queue first.
CMD ["uvicorn", "basivo_orch.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", "--no-server-header", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
