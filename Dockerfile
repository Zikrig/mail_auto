FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Цикл в ENTRYPOINT — не зависит от прав на смонтированный entrypoint.sh
# Код, data/, .env и creds.json монтируются при запуске (docker-compose или -v .:/app)
ENTRYPOINT ["sh", "-c", "while true; do python /app/mail_autoresponder.py; echo 'Следующий запуск через 2 минуты...'; sleep 120; done"]
