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

# Отключаем буферизацию вывода Python, чтобы логи сразу попадали в docker logs
ENV PYTHONUNBUFFERED=1

# Основной бесконечный цикл теперь внутри mail_autoresponder.py
ENTRYPOINT ["python", "/app/mail_autoresponder.py"]
