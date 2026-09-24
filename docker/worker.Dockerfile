FROM ghcr.io/astral-sh/uv:0.9.7@sha256:ba4857bf2a068e9bc0e64eed8563b065908a4cd6bfb66b531a9c424c8e25e142 AS uv
FROM ghcr.io/astral-sh/uv:0.11.6@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754 AS hermes_uv
FROM node:22-bookworm-slim@sha256:48e4b67d85f87bd551df43704e24d252f56cc5f8e9718841aace50f19948f0f9 AS node
FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
ARG HERMES_COMMIT=38c9611791d3c8eccee5bb3fdad8075ec1d58565
LABEL org.opencontainers.image.source="https://github.com/NousResearch/hermes-agent"
LABEL autocoder.hermes.commit=$HERMES_COMMIT
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && \
    apt-get update && apt-get install -y --no-install-recommends git curl ca-certificates ripgrep build-essential && \
    rm -rf /var/lib/apt/lists/*
RUN mkdir /opt/hermes && curl -fL --retry 5 "https://codeload.github.com/NousResearch/hermes-agent/tar.gz/${HERMES_COMMIT}" | \
    tar -xz --strip-components=1 -C /opt/hermes
WORKDIR /opt/hermes
COPY --from=hermes_uv /uv /usr/local/bin/uv
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --python /usr/local/bin/python3 --no-python-downloads
COPY src /opt/autocoder/src
ENV PYTHONPATH=/opt/autocoder/src:/opt/hermes PYTHONDONTWRITEBYTECODE=1 UV_PYTHON_DOWNLOADS=never
RUN useradd --uid 1000 --create-home worker
USER 1000:1000
WORKDIR /workspace
CMD ["sleep", "infinity"]
