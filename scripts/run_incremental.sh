#!/bin/bash
# Інкрементальний прогін для планувальника.
# Викликається launchd; усе пишеться в logs/scheduler.log.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

LOG="logs/scheduler.log"
mkdir -p logs

# Проста ротація: тримаємо лог у межах 5 МБ.
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 5242880 ]; then
  mv "$LOG" "$LOG.1"
fi

echo "=== $(date '+%Y-%m-%d %H:%M:%S') старт інкрементального прогону ===" >>"$LOG"
.venv/bin/python cli.py scrape --trigger schedule >>"$LOG" 2>&1
code=$?

# Порція перевірки актуальності: за кілька циклів обходить усю базу, не
# створюючи сплеску навантаження на джерела.
echo "--- перевірка актуальності ---" >>"$LOG"
.venv/bin/python cli.py verify --limit 150 >>"$LOG" 2>&1 || true

# Зведення дублів: без цього кількість унікальних квартир застаріває між
# ручними запусками.
echo "--- дедуплікація ---" >>"$LOG"
.venv/bin/python cli.py dedup >>"$LOG" 2>&1 || true
echo "=== $(date '+%Y-%m-%d %H:%M:%S') завершено, код $code ===" >>"$LOG"
exit $code
