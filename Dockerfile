# Ubuntu is required by Playwright's Firefox dependency installer.
# Pinned to 24.04 LTS deliberately: `ubuntu:latest` floats to 26.04, for which
# Playwright cannot install Firefox deps. Byparr hit the same wall.
FROM ubuntu:24.04

# org.opencontainers.image.source is what links the GHCR package back to this repo,
# giving it a README and a source link on the package page.
LABEL org.opencontainers.image.source="https://github.com/DulsaraNethmin/Minter" \
      org.opencontainers.image.description="Gets you past Cloudflare's browser challenge — as a clearance cookie or the rendered page." \
      org.opencontainers.image.licenses="MIT"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    HOME=/home/app \
    PATH=/app/.venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# Create the runtime user up front and build everything as that user. A recursive
# chown after the fact would rewrite every file in .venv and the browser cache,
# and Docker would store a second full copy of both (~250 MB wasted).
# Ubuntu 24.04 ships a default `ubuntu` user already holding UID 1000, so it has to
# go before we can claim that UID. We want 1000 specifically: it matches PUID on the
# Pi, keeping file ownership consistent with the rest of the stack.
RUN userdel --remove ubuntu 2>/dev/null || true \
    && useradd --uid 1000 --create-home --home-dir /home/app app \
    && mkdir -p /app \
    && chown 1000:1000 /app

WORKDIR /app
USER 1000

# Dependency layer first so source edits do not invalidate it.
# uv's cache is build-only; leaving it behind costs ~225 MB in the final image.
COPY --chown=1000:1000 pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project \
    && uv cache clean

COPY --chown=1000:1000 src/ ./src/
COPY --chown=1000:1000 main.py ./
RUN uv sync --frozen --no-dev \
    && uv cache clean

# Firefox's system libraries. Needs root for apt; the venv it reads is owned by
# 1000 but readable, so `uv run` still resolves.
USER root
RUN uv run playwright install-deps firefox \
    && rm -rf /var/lib/apt/lists/*
USER 1000

# Bake the patched Firefox into the image rather than downloading at first request —
# a cold start would otherwise pull ~230 MB before the first mint can be served.
# Runs as 1000 so the cache lands correctly owned, with no chown needed.
RUN python main.py --init

EXPOSE 8191

# Slow by design: this drives a real browser end to end, so keep the interval long.
HEALTHCHECK --interval=15m --timeout=90s --start-period=60s --retries=2 \
    CMD curl -fs http://localhost:8191/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "main.py"]
