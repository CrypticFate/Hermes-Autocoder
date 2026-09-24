# Concierge: persistent Hermes (same pinned source as builders) with mem0 OSS memory and no code tools.
FROM ghcr.io/astral-sh/uv:0.11.6@sha256:b1e699368d24c57cda93c338a57a8c5a119009ba809305cc8e86986d4a006754 AS hermes_uv
FROM python:3.13-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
ARG HERMES_COMMIT=38c9611791d3c8eccee5bb3fdad8075ec1d58565
ARG MEM0_VERSION=2.2.0
ARG FASTEMBED_VERSION=0.8.1
LABEL org.opencontainers.image.source="https://github.com/NousResearch/hermes-agent"
LABEL autocoder.hermes.commit=$HERMES_COMMIT autocoder.role=concierge
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN mkdir /opt/hermes && curl -fL --retry 5 "https://codeload.github.com/NousResearch/hermes-agent/tar.gz/${HERMES_COMMIT}" | \
    tar -xz --strip-components=1 -C /opt/hermes
WORKDIR /opt/hermes
COPY --from=hermes_uv /uv /usr/local/bin/uv
# The `messaging` extra provides the Telegram gateway adapter (python-telegram-bot).
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --extra messaging --python /usr/local/bin/python3 --no-python-downloads
# Run from /opt/hermes, so Hermes's [tool.uv] exclude-newer="14 days" applies. Exempt our exact pins (the pin
# bump is the review) so a fresh release does not brick the build; unpinned transitives stay quarantined.
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --python /opt/hermes/.venv/bin/python \
    --exclude-newer-package mem0ai=false --exclude-newer-package fastembed=false \
    "mem0ai==${MEM0_VERSION}" "fastembed==${FASTEMBED_VERSION}" "psycopg[binary,pool]>=3.2,<4" "pyyaml>=6,<7"
# Bake the embedding model: the concierge has no egress at runtime.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
RUN HF_HUB_OFFLINE=0 /opt/hermes/.venv/bin/python -c \
    "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/opt/fastembed')" && \
    chmod -R a+rX /opt/fastembed
COPY docker/concierge/ /opt/concierge/
COPY scripts/mem0_admin.py /opt/concierge/mem0_admin.py
RUN chmod 755 /opt/concierge/entrypoint.sh && useradd --uid 1000 --create-home concierge && \
    mkdir -p /home/concierge/.hermes && chown -R 1000:1000 /home/concierge
ENV PATH="/opt/hermes/.venv/bin:$PATH" HERMES_HOME=/home/concierge/.hermes PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/opt/hermes
USER 1000:1000
WORKDIR /home/concierge
ENTRYPOINT ["/opt/concierge/entrypoint.sh"]
