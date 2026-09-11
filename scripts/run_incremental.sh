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

# Порція перевірки актуальності. Число — НА КОЖЕН САЙТ, а не на всіх разом:
# черги незалежні, бо ліміт запитів у кожного сайту свій. Навантаження на
# окремий сайт від цього не зростає — зростає лише сумарна пропускна
# здатність, і саме за рахунок сайтів, які досі не перевірялись зовсім.
echo "--- перевірка актуальності ---" >>"$LOG"
.venv/bin/python cli.py verify --limit 150 >>"$LOG" 2>&1 || true

# Зведення дублів: без цього кількість унікальних квартир застаріває між
# ручними запусками.
echo "--- дедуплікація ---" >>"$LOG"
.venv/bin/python cli.py dedup >>"$LOG" 2>&1 || true

# Контроль якості: щодня легка рутина, у понеділок — глибша з перерахунком
# порогів, першого числа місяця — повна звірка.
DOW=$(date +%u); DOM=$(date +%d)
if [ "$DOM" = "01" ]; then ROUTINE=monthly
elif [ "$DOW" = "1" ]; then ROUTINE=weekly
else ROUTINE=daily; fi
echo "--- контроль якості ($ROUTINE) ---" >>"$LOG"
.venv/bin/python cli.py quality "$ROUTINE" >>"$LOG" 2>&1 || true
# Зворотний відлік до моменту, коли аналітика зможе показати прогноз і строк
# продажу. Нічого не рахує наперед — просто робить видимим, скільки лишилось.
echo "--- готовність аналітики ---" >>"$LOG"
.venv/bin/python cli.py analytics forecast >>"$LOG" 2>&1 || true

echo "=== $(date '+%Y-%m-%d %H:%M:%S') завершено, код $code ===" >>"$LOG"
exit $code
