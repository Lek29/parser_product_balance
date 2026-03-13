FROM python:3.12-slim

# unrar нужен rarfile для распаковки .rar вложений из почты
RUN apt-get update && apt-get install -y --no-install-recommends \
        unrar-free \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# storage и temp монтируются как volume — создаём заранее
RUN mkdir -p storage temp

CMD ["python", "main.py"]
