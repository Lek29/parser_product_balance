FROM python:3.12-slim

# GHCR связывает пакет с репозиторием по этой метке, иначе GITHUB_TOKEN
# получает denied: permission_denied: write_package.
LABEL org.opencontainers.image.source="https://github.com/Lek29/parser_product_balance"

# unrar-free / unrar: нужен rarfile для распаковки .rar вложений с почты
RUN apt-get update && apt-get install -y --no-install-recommends \
        unrar-free \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    UV_PYTHON_DOWNLOADS=0 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Бинарник uv (~55 МБ) монтируется только на время RUN и не попадает в финальный образ.
COPY pyproject.toml uv.lock ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.11.19,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-editable

COPY . .

# Предкомпилируем .pyc для кода самого приложения, чтобы при холодном старте на VPS
# Python не парсил .py файлы в AST каждый раз заново.
RUN mkdir -p storage temp && python -m compileall -q /app

CMD ["python", "main.py"]
