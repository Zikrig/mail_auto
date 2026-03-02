FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY entrypoint.sh .
RUN chmod +x /app/entrypoint.sh

# Код, data/, .env и creds.json монтируются при запуске (docker-compose или -v .:/app)
ENTRYPOINT ["/app/entrypoint.sh"]
