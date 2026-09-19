#!/bin/bash
# Встановлення від звичайного користувача (без sudo). Можна запускати повторно.
#
#     bash ~/realty/deploy/fedora/install.sh            # код, залежності, юніти
#     bash ~/realty/deploy/fedora/install.sh --enable   # + увімкнути служби
#
# Порядок переїзду: install → пробний прогін на тестовій базі → перенесення
# бази → install --enable. Службу збору не вмикаємо, доки база не на місці.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT="$(pwd)"
UNITS="$HOME/.config/systemd/user"

echo "== залежності Python"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

echo "== Chromium для Playwright"
.venv/bin/playwright install chromium
missing=$(ldd "$(find ~/.cache/ms-playwright -type f -name chrome-headless-shell | head -1)" \
          | grep -c "not found" || true)
echo "   бракує бібліотек: $missing"

echo "== cloudflared"
if [ ! -x "$HOME/.local/bin/cloudflared" ]; then
  mkdir -p "$HOME/.local/bin"
  curl -fsSL --max-time 300 -o "$HOME/.local/bin/cloudflared.part" \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
  chmod +x "$HOME/.local/bin/cloudflared.part"
  mv "$HOME/.local/bin/cloudflared.part" "$HOME/.local/bin/cloudflared"
fi
"$HOME/.local/bin/cloudflared" --version

echo "== .env"
if [ -f .env ]; then
  chmod 600 .env
  for key in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID AUTH_USER AUTH_PASSWORD; do
    grep -q "^$key=." .env && echo "   $key: задано" || echo "   $key: НЕМАЄ"
  done
else
  echo "   .env ще немає — його кладе людина вручну (див. .env.example)"
fi

echo "== юніти systemd (користувацькі)"
mkdir -p "$UNITS"
cp deploy/fedora/systemd/*.service deploy/fedora/systemd/*.timer "$UNITS/"
systemctl --user daemon-reload

if [ "${1:-}" = "--enable" ]; then
  if [ -f data/COLLECTOR_OFF ]; then
    echo "УВАГА: data/COLLECTOR_OFF існує — збір на цій машині вимкнено свідомо." >&2
    exit 1
  fi
  systemctl --user enable --now realty-web.service realty-tunnel.service \
    realty-cycle.timer realty-backup.timer realty-watchdog.timer
  systemctl --user list-timers 'realty-*' --no-pager
fi
echo "Готово."
