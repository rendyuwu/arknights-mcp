# Container image for the Arknights Intelligence MCP private remote (§T55).
#
# Code-only image (§V16): it ships the project code + locked dependencies and
# NOTHING else. No raw snapshots, no prebuilt SQLite build, no game content are
# baked in -- the promoted build is supplied at runtime as a read-only mounted
# volume (see docker-compose.yml). .dockerignore keeps data/, *.sqlite, and
# snapshots out of the build context so they can never leak into a layer.
#
# Build (from the repo root, which is the build context):
#   docker build -f deploy/docker/Dockerfile -t arknights-mcp .

FROM python:3.12-slim AS build

# Pinned uv for a reproducible, frozen dependency install (§C: uv + lockfile).
COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first against just the manifest + lockfile for layer caching.
# --frozen refuses to touch uv.lock: the image resolves the exact locked
# versions or the build fails (§V25/§C). --no-dev omits test/lint tooling.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now the project source. Copied after deps so a code-only change reuses the
# dependency layer.
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime

# Run as an unprivileged user; never root (defense in depth for the read-only
# server -- §V1/§V2).
RUN useradd --system --no-create-home --shell /usr/sbin/nologin arknights
WORKDIR /app

# The built virtualenv + project code from the build stage.
COPY --from=build /app /app

ENV PATH="/app/.venv/bin:$PATH"
# The promoted build lives on a mounted volume, opened read-only (§V2). config
# must set [mcp.remote] behind_proxy = true so the loopback bind still enforces
# the §V9 gate + bearer validation behind the TLS proxy (§V40).
VOLUME ["/app/data"]
EXPOSE 8000

USER arknights

# serve opens the promoted build read-only and never fetches source data at
# query time (§V1). Secrets/OIDC descriptors arrive via env (§V12/§I.env), never
# baked into the image.
ENTRYPOINT ["arknights-mcp", "--config", "/app/config.toml", "serve", "--transport", "streamable-http"]
