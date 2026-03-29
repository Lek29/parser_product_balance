FROM python:3.12-slim

# unrar-free / unrar: нужны rarfile для распаковки .rar вложений из почты
RUN apt-get update && apt-get install -y --no-install-recommends \
        unrar-free \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p storage temp

CMD ["python", "main.py"]
