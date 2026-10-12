# Version-pinned runtime components: Python 3.13, uv 0.11.19, Litestream 0.5.16.
# Image digests are intentionally left to the deployment's image policy so security
# rebuilds can update Debian patches without silently changing these tool versions.
FROM ghcr.io/astral-sh/uv:0.11.19 AS uv
FROM python:3.13.12-slim-bookworm AS build
COPY --from=uv /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM litestream/litestream:0.5.16 AS litestream
FROM python:3.13.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY --from=build /app /app
COPY --from=litestream /usr/local/bin/litestream /usr/local/bin/litestream
COPY deploy/litestream.yml /etc/litestream.yml
COPY deploy/docker-entrypoint.sh /usr/local/bin/vocab-entrypoint
RUN apt-get update && apt-get install --no-install-recommends -y util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system vocab && useradd --system --gid vocab --home-dir /app vocab \
    && chmod 0555 /usr/local/bin/vocab-entrypoint && mkdir -p /data && chown vocab:vocab /data
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/vocab-entrypoint"]
