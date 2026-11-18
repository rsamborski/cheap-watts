# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

LABEL org.opencontainers.image.title="cheap-watts" \
      org.opencontainers.image.description="Headless Home Energy Management System optimizing Deye battery storage and EV charging with Pstryk dynamic tariffs." \
      org.opencontainers.image.source="https://github.com/rsamborski/cheap-watts" \
      org.opencontainers.image.licenses="Apache-2.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TZ=Europe/Warsaw \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 watts \
    && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash watts

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

RUN uv sync --frozen --no-dev \
    && mkdir -p /app/data /app/logs \
    && chown -R watts:watts /app

USER watts

VOLUME ["/app/data", "/app/logs"]

ENTRYPOINT ["cheap-watts"]
