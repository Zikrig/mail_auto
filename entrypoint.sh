#!/bin/sh
set -e
echo "Автоответчик запущен. Проверка почты каждые 2 минуты."
while true; do
  python /app/mail_autoresponder.py
  echo "Следующий запуск через 2 минуты..."
  sleep 120
done
