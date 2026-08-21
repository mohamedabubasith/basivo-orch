# Built for arm64 (Graviton). The dev machine is Apple Silicon, so this is a
# native build both ways — no emulation, no cross-compilation.

FROM python:3.12-slim-bookworm AS deps

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the lockfile in their own layer, so editing source
# does not re-resolve and re-download the world on every build. Its own STAGE,
# not just its own layer, because the worker image needs this venv (for the
# playwright CLI) without depending on our source — see `worker-tools`.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev


FROM deps AS builder

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


# ---------------------------------------------------------------------------
# The worker image
# ---------------------------------------------------------------------------
#
# Everything the render path needs, and nothing the API does: FFmpeg, Node for
# HyperFrames, Chromium for the frame capture and the poster screenshots, and
# the voice model.
#
# A separate image rather than one fat one, because the API is redeployed
# constantly and this adds roughly 700MB. On a VPS that meters inbound traffic,
# pulling that on every API deploy is a bill for nothing.
#
# **The heavy layers come before the app on purpose.** This used to be
# `FROM runtime`, which contains our source — so changing one line of Python
# invalidated apt, 363MB of npm, the Chromium download and the model, and every
# deploy paid for all of it again. Nothing below depends on our code until the
# final COPY, so a code change now rebuilds one layer.

FROM python:3.12-slim-bookworm AS worker-tools

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        gnupg \
        curl \
    # Node 22 for the HyperFrames CLI. From nodesource rather than Debian,
    # which ships 18 — HyperFrames requires 22.
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g hyperframes@0.8.3 \
    && npm cache clean --force \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 basivo

# The venv comes from `deps`, not from `builder`: it holds the playwright CLI
# needed for the next step, and taking it from the code-free stage is what
# keeps the Chromium download out of every deploy.
WORKDIR /app
COPY --from=deps /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Chromium and the shared libraries it needs. `playwright install --with-deps`
# installs both, and into a path every user can read — the default is the
# invoking user's home, which the `basivo` user cannot see.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright

# HyperFrames renders with puppeteer-core, which bundles NO browser: left to
# itself it downloads Chrome 152 from Google on the first render, into every
# fresh container, and fails outright if that download is blocked. So it is
# pointed at the browser we just installed.
#
# Not the download it would prefer, for a reason: its own table of
# chrome-headless-shell builds has no linux/arm64 entry, because Chrome for
# Testing does not publish one. Pointing at Playwright's Chromium is the only
# choice that works the same on x86_64 and ARM.
#
# The headless shell is preferred when Playwright ships one, because it
# supports HeadlessExperimental.beginFrame — HyperFrames' fast capture path.
# Plain Chromium works too and falls back to screenshots, which is slower but
# correct.
#
# The `test -x` is what makes an unresolved symlink fail the BUILD instead of
# the first render — the difference between a broken image and a container that
# looks healthy until someone asks for a video.
RUN shell="$(find /opt/playwright -type f -name headless_shell -perm -u+x | head -1)"; \
    chrome="$(find /opt/playwright -type f -name chrome -perm -u+x | head -1)"; \
    mkdir -p /opt/basivo; \
    ln -sf "${shell:-$chrome}" /opt/basivo/chrome; \
    test -x /opt/basivo/chrome || { echo "no chromium under /opt/playwright" >&2; exit 1; }; \
    echo "hyperframes browser: $(readlink -f /opt/basivo/chrome)"
ENV HYPERFRAMES_BROWSER_PATH=/opt/basivo/chrome

# The voice model: two files, ~109MB. Fetched with curl from pinned URLs rather
# than through our own `fetch_model()`, because calling into the package would
# make this layer depend on our source — which is the whole thing this stage
# exists to avoid. `test_the_dockerfile_and_the_code_agree_on_the_model` keeps
# these URLs identical to MODEL_SOURCES.
ENV BASIVO_SPEECH_MODEL_DIR=/opt/basivo/speech
RUN mkdir -p /opt/basivo/speech \
    && curl -fsSL -o /opt/basivo/speech/kokoro-timestamped-q8f16.onnx \
        "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX-timestamped/resolve/main/onnx/model_q8f16.onnx" \
    && curl -fsSL -o /opt/basivo/speech/voices-v1.0.bin \
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin" \
    && chmod -R a+rX /opt/basivo \
    && ls -l /opt/basivo/speech

# Everything the render path shells out to, checked once here rather than on
# the first render — a missing tool should fail the build, not a queued run.
RUN set -e; \
    ffmpeg -version  | head -1; \
    ffprobe -version | head -1; \
    node --version; \
    hyperframes --help >/dev/null && echo "hyperframes ok"; \
    test -s /opt/basivo/speech/voices-v1.0.bin && echo "voice model ok"


FROM worker-tools AS worker

# The only layer our source touches. Everything above stays cached across a
# code change, which is the difference between a one minute deploy and fifteen.
COPY --from=builder --chown=basivo:basivo /app /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER basivo

# No HTTP server, so the API's healthcheck would fail forever and mark this
# container unhealthy. Liveness for a worker is "the process is up and the
# claim query works", which the run log already tells you.
HEALTHCHECK NONE

CMD ["python", "-m", "basivo_orch.worker"]
