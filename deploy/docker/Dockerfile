# Container image for the Arknights Intelligence MCP private remote.
#
# Code-only image: it ships the project code + locked dependencies and
# NOTHING else. No raw snapshots, no prebuilt SQLite build, no game content are
# baked in -- the promoted build is supplied at runtime as a read-only mounted
# volume (see docker-compose.yml). .dockerignore keeps data/, *.sqlite, and
# snapshots out of the build context so they can never leak into a layer.
#
# Build (from the repo root, which is the build context):
#   docker build -f deploy/docker/Dockerfile -t arknights-mcp .
#
# One image, both transports. ENTRYPOINT is the console script plus the
# config path only; the transport lives in CMD, so it is overridable per `docker
# run` without a second image:
#
#   # private remote (default CMD) -- see docker-compose.yml
#   docker run --rm -v "$PWD/data:/app/data:ro" -v "$PWD/config.toml:/app/config.toml:ro" \
#     arknights-mcp
#
#   # local stdio for an MCP client (-i keeps stdin open; NEVER pass -t)
#   docker run --rm -i -v "$PWD/data:/app/data:ro" -v "$PWD/config.toml:/app/config.toml:ro" \
#     arknights-mcp serve --transport stdio

FROM python:3.12-slim AS build

# Pinned uv for a reproducible, frozen dependency install (uv + lockfile).
COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first against just the manifest + lockfile for layer caching.
# --frozen refuses to touch uv.lock: the image resolves the exact locked
# versions or the build fails. --no-dev omits test/lint tooling.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Now the project source. Copied after deps so a code-only change reuses the
# dependency layer.
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime

# Run as an unprivileged user; never root (defense in depth for the read-only
# server).
RUN useradd --system --no-create-home --shell /usr/sbin/nologin arknights
WORKDIR /app

# The built virtualenv + project code from the build stage.
COPY --from=build /app /app

ENV PATH="/app/.venv/bin:$PATH"
# stdio frames the MCP JSON-RPC stream on stdout. Unbuffered keeps a reply
# from sitting in a pipe buffer waiting for the block to fill -- an MCP client
# reading the pipe would otherwise stall -- and keeps stderr notices in real time.
ENV PYTHONUNBUFFERED=1
# The promoted build lives on a mounted volume, opened read-only. For the
# streamable-http transport, config must set [mcp.remote] behind_proxy = true so
# the loopback bind still enforces the HTTPS + auth gate + bearer validation behind the TLS
# proxy. stdio needs none of that: it is a local pipe with no listener,
# so no bind, no bearer, and no OIDC env are involved.
VOLUME ["/app/data"]
EXPOSE 8000

USER arknights

# serve opens the promoted build read-only and never fetches source data at
# query time. Secrets/OIDC descriptors arrive via env, never
# baked into the image.
#
# The transport is deliberately NOT part of ENTRYPOINT: keeping it in CMD is what
# lets `docker run ... serve --transport stdio` reuse this image (one
# shared core, two transports, and now one image for both). ENTRYPOINT still pins
# the config path so an override cannot silently pick up a different config.
ENTRYPOINT ["arknights-mcp", "--config", "/app/config.toml"]
CMD ["serve", "--transport", "streamable-http"]
