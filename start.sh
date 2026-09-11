#!/bin/bash
# Запуск сайту. Просто: ./start.sh
# Зупинити — Ctrl+C у цьому ж вікні.
cd "$(dirname "$0")" || exit 1

PORT="${1:-8000}"

# Якщо порт уже зайнятий — найімовірніше сайт уже запущений.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Порт $PORT уже зайнятий — схоже, сайт уже працює."
  echo "Відкрийте http://localhost:$PORT"
  echo "Якщо це щось інше, запустіть на іншому порту: ./start.sh 8001"
  exit 0
fi

echo "Запускаю сайт…"
echo
echo "  Моніторинг:   http://localhost:$PORT"
echo "  В обробці:    http://localhost:$PORT/processing"
echo "  Аналітика:    http://localhost:$PORT/analytics"
echo "  Стан системи: http://localhost:$PORT/status"
echo
echo "Зупинити — Ctrl+C."
echo

# Відкриваємо браузер через секунду, коли сервер устигне піднятись.
( sleep 1.5; open "http://localhost:$PORT" >/dev/null 2>&1 ) &

exec .venv/bin/python cli.py serve --port "$PORT"
