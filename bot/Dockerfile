FROM python:3.12-slim

# Debian's ffmpeg is built with libass; Montserrat has Cyrillic, unlike the default fonts
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-montserrat \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY sublya ./sublya
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH \
    DATA_DIR=/data \
    SUBLYA_FONTS_DIR=/usr/share/fonts/opentype/montserrat
CMD ["sublya-bot"]
