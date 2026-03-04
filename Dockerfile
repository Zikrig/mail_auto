FROM python:3.11-slim

# Настраиваем часовой пояс контейнера на московский (+3)
ENV TZ=Europe/Moscow
RUN apt-get update && apt-get install -y tzdata \
    && ln -snf /usr/share/zoneinfo/Europe/Moscow /etc/localtime \
    && echo "Europe/Moscow" > /etc/timezone \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Цикл в ENTRYPOINT — не зависит от прав на смонтированный entrypoint.sh
# Код, data/, .env и creds.json монтируются при запуске (docker-compose или -v .:/app)
ENTRYPOINT ["sh", "-c", "while true; do python /app/mail_autoresponder.py; echo 'Следующий запуск через 2 минуты...'; sleep 120; done"]
