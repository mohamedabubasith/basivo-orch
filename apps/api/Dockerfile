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
        # The jail every coding agent runs in: a mount and PID namespace of
        # its own, with only the workspace writable. See flows/nodes/jail.py.
        bubblewrap \
    # Node 22 for the video renderer and the coding agents. From nodesource
    # rather than Debian, which ships 18.
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    # Three coding agents, pinned. Claude Code and Codex run on the
    # workspace's own credential; OpenCode is the free one, and the reason a
    # workspace with no key at all can still have its code fixed.
    && npm install -g \
        @anthropic-ai/claude-code@2.1.259 \
        @openai/codex@0.154.0 \
        opencode-ai@1.15.13 \
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

# Fonts, because this renders wedding invitations in India.
#
# Playwright's dependency set brings Liberation and nothing else, so a name in
# Tamil or Devanagari renders as a row of empty boxes — a failure that looks
# like a bug in the customer's phone rather than a missing package, and one
# nobody notices until an invitation goes out. `fonts-indic` covers Devanagari,
# Tamil, Telugu, Kannada, Malayalam, Gujarati, Bengali and Gurmukhi.
#
# EB Garamond is here for a different reason: an invitation set in DejaVu Serif
# looks like a spreadsheet, and the default serif on a bare Linux container is
# DejaVu Serif.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-indic \
        fonts-noto-core \
        fonts-ebgaramond \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f >/dev/null 2>&1 || true

# The voice model: two files, ~109MB. Fetched with curl from pinned URLs rather
# than through our own `fetch_model()`, because calling into the package would
# make this layer depend on our source, which is the whole thing this stage
# exists to avoid.
#
# It sits ABOVE the renderer's npm install on purpose. It depends on nothing
# but curl, and below that layer every edit to remotion_project/package.json
# re-downloaded 109MB of voice model for no reason.
#
# `test_the_dockerfile_and_the_code_agree_on_the_model` keeps these URLs
# identical to MODEL_SOURCES.
ENV BASIVO_SPEECH_MODEL_DIR=/opt/basivo/speech
RUN mkdir -p /opt/basivo/speech \
    && curl -fsSL -o /opt/basivo/speech/kokoro-timestamped-q8f16.onnx \
        "https://huggingface.co/onnx-community/Kokoro-82M-v1.0-ONNX-timestamped/resolve/main/onnx/model_q8f16.onnx" \
    && curl -fsSL -o /opt/basivo/speech/voices-v1.0.bin \
        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin" \
    && chmod -R a+rX /opt/basivo \
    && ls -l /opt/basivo/speech

# The video renderer, installed once into the image.
#
# node_modules is baked in on purpose: a render that runs `npm install` first
# is a render that fails the day the registry is slow, and it would do it
# inside a subprocess that deliberately has no network reason to exist. Only
# the manifest is copied here, so a change to our own Python leaves this layer
# cached.
COPY basivo_orch/flows/nodes/remotion_project/package.json \
     /opt/basivo/remotion/package.json
RUN cd /opt/basivo/remotion \
    && npm install --omit=dev --no-audit --no-fund \
    && npm cache clean --force \
    && chmod -R a+rX /opt/basivo/remotion
ENV BASIVO_REMOTION_NODE_MODULES=/opt/basivo/remotion/node_modules

# The App Builder's toolchain, installed once for the same reasons. Vite and
# Tailwind are dev dependencies here because a build needs them, so this one
# installs everything rather than omitting them.
COPY basivo_orch/appbuilder/template/package.json \
     /opt/basivo/app-template/package.json
RUN cd /opt/basivo/app-template \
    && npm install --no-audit --no-fund \
    && npm cache clean --force \
    && chmod -R a+rX /opt/basivo/app-template
ENV BASIVO_APP_NODE_MODULES=/opt/basivo/app-template/node_modules

# The browser Remotion draws frames with. Downloaded at build time into a path
# every user can read, because the alternative is every fresh container
# downloading it on its first render, and failing outright where that download
# is blocked.
#
# Not Playwright's Chromium, which is installed above for the poster path:
# Remotion pins a Chrome Headless Shell build it has tested against, and mixing
# the two produces frame differences that are invisible until a customer says
# their video looks wrong.
# The download lands under XDG_CACHE_HOME, so it is pinned to a path every
# user can read. Left at the default it goes to the building user's home,
# which the `basivo` user cannot see, and the first render downloads it again
# as that user, into a container layer that does not persist.
ENV XDG_CACHE_HOME=/opt/basivo/cache
RUN mkdir -p "$XDG_CACHE_HOME" \
    && cd /opt/basivo/remotion \
    && node -e "require('@remotion/renderer').ensureBrowser().then(() => console.log('remotion browser ok'))" \
    && chmod -R a+rX "$XDG_CACHE_HOME" /opt/basivo/remotion

# A cold OpenCode home costs two things on its first real session: a one time
# sqlite migration, and a package install it performs for itself. Every run
# gets a throwaway HOME by design, so both are warmed here, once.
#
# Best effort, deliberately. The migration is offline and always works; the
# package install happens inside a real session, which needs a model to
# answer. An image build must not depend on somebody else's inference
# endpoint being up, so a session that does not complete leaves a note in the
# build log and nothing else. The worker warms what is missing on its first
# turn and caches it (see `_write_opencode_home`), which is where the network
# is known to work because a turn is already talking to a model.
ENV BASIVO_OPENCODE_HOME_TEMPLATE=/opt/basivo/opencode
RUN set -e; \
    mkdir -p /opt/basivo/opencode/work; \
    cd /opt/basivo/opencode/work; \
    HOME=/opt/basivo/opencode \
    XDG_DATA_HOME=/opt/basivo/opencode/data \
    XDG_CONFIG_HOME=/opt/basivo/opencode/config \
    XDG_CACHE_HOME=/opt/basivo/opencode/cache \
    timeout 180 opencode run --pure --dir . \
        -m "${BASIVO_OPENCODE_MODEL:-opencode/big-pickle}" \
        "Reply with the single word OK." || echo "opencode warm session skipped"; \
    test -d /opt/basivo/opencode/data/opencode \
        || (echo "opencode did not create its data directory" && exit 1); \
    if [ -d /opt/basivo/opencode/config/opencode/node_modules ]; then \
        echo "opencode packages warmed"; \
    else \
        echo "opencode packages not warmed at build; the first turn will do it"; \
    fi; \
    rm -rf /opt/basivo/opencode/work; \
    chmod -R a+rX /opt/basivo/opencode


# Everything the render path shells out to, checked once here rather than on
# the first render — a missing tool should fail the build, not a queued run.
RUN set -e; \
    ffmpeg -version  | head -1; \
    ffprobe -version | head -1; \
    node --version; \
    test -d /opt/basivo/remotion/node_modules/remotion && echo "remotion ok"; \
    test -d /opt/basivo/app-template/node_modules/vite && echo "app builder ok"; \
    claude --version; \
    codex --version; \
    opencode --version; \
    bwrap --version; \
    test -d /opt/basivo/opencode/data/opencode && echo "opencode home warmed"; \
    test -s /opt/basivo/speech/voices-v1.0.bin && echo "voice model ok"


FROM worker-tools AS worker

# The only layer our source touches. Everything above stays cached across a
# code change, which is the difference between a one minute deploy and fifteen.
COPY --from=builder --chown=basivo:basivo /app /app

# A coding agent runs only inside the jail on this image. If the container
# cannot create user namespaces the turn fails with a message that names the
# fix, rather than running an agent that can read /proc/1/environ.
ENV BASIVO_AGENT_JAIL=required

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER basivo

# No HTTP server, so the API's healthcheck would fail forever and mark this
# container unhealthy. Liveness for a worker is "the process is up and the
# claim query works", which the run log already tells you.
HEALTHCHECK NONE

CMD ["python", "-m", "basivo_orch.worker"]
