FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

WORKDIR /app
# pacat: the voice channel's microphone and speaker, through the host's PipeWire.
RUN apt-get update && apt-get install -y --no-install-recommends pulseaudio-utils && rm -rf /var/lib/apt/lists/*
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY homebot ./homebot

# /data is created here so the named volume inherits the non-root owner.
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
# VOICE_DIR holds the voice models, downloaded on first start.
ENV PATH="/app/.venv/bin:$PATH" DB_PATH=/data/homebot.db VOICE_DIR=/data/voice
CMD ["python", "-m", "homebot"]
