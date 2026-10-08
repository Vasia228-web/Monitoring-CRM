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

echo "== rclone (копія бекапу в хмару)"
if [ ! -x "$HOME/.local/bin/rclone" ]; then
  tmp=$(mktemp -d); pushd "$tmp" >/dev/null
  VER=$(curl -fsSL --max-time 60 https://downloads.rclone.org/version.txt | awk '{print $2}')
  ZIP="rclone-$VER-linux-amd64.zip"
  curl -fsSL --max-time 300 -O "https://downloads.rclone.org/$VER/$ZIP"
  curl -fsSL --max-time 60 -O "https://downloads.rclone.org/$VER/SHA256SUMS"
  want=$(grep " $ZIP$" SHA256SUMS | cut -d' ' -f1)
  have=$(sha256sum "$ZIP" | cut -d' ' -f1)
  [ "$want" = "$have" ] || { echo "   контрольна сума rclone не збіглась — пропускаю"; }
  if [ "$want" = "$have" ]; then
    unzip -qo "$ZIP"; mkdir -p "$HOME/.local/bin"
    install -m 755 "rclone-$VER-linux-amd64/rclone" "$HOME/.local/bin/rclone"
  fi
  popd >/dev/null; rm -rf "$tmp"
fi
"$HOME/.local/bin/rclone" version 2>/dev/null | head -1 || echo "   rclone не встановлено"
if "$HOME/.local/bin/rclone" listremotes 2>/dev/null | grep -q .; then
  echo "   налаштовані сховища: $($HOME/.local/bin/rclone listremotes | tr '\n' ' ')"
else
  echo "   сховище ще не під'єднано — це робить людина одноразово:"
  echo "   rclone config create gdrive drive scope=drive.file"
fi

echo "== .env"
if [ -f .env ]; then
  chmod 600 .env
  for key in TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID AUTH_USER AUTH_PASSWORD; do
    grep -q "^$key=." .env && echo "   $key: задано" || echo "   $key: НЕМАЄ"
  done
else
  echo "   .env ще немає — його кладе людина вручну (див. .env.example)"
fi

# Конфіги перевіряються ДО того, як служби підхоплять новий код (конвенція
# config/README.md, п. 6): зламаний TOML або REALTY_CONFIG_DIR у .env зупиняють
# встановлення тут (set -e), а не сайт на першому запиті.
echo "== конфіги (config/)"
.venv/bin/python cli.py config check

# Схема бази (Блок 2, D50): лише ПЛАН, нічого не пише. Застосування — окремо,
# між циклами, після бекапу: `.venv/bin/python cli.py db migrate` (бере замок
# циклу, звіряє кількості й суми, integrity_check, foreign_key_check).
echo "== схема бази (план, без змін)"
.venv/bin/python cli.py db migrate --dry-run || echo "   план не вдалося скласти — див. вище"

# Пріоритети в юнітах (Блок 2, D50) діють без root, але якщо ядро чи systemd
# відмовить (EPERM), служба сайту не стартувала б зовсім. Тому спершу пробний
# запуск із тими самими налаштуваннями; відмова — юніти не встановлюю.
echo "== пріоритети служб: пробний запуск без root"
probe() { systemd-run --user --quiet --wait --collect "$@" /bin/true; }
# Чотири набори: сайт, цикл, процес перевірки realty-lookup@ (там ще й ліміти пам'яті)
# і нічний диригент realty-night (E9, D53: ще й IOWeight — без делегування io systemd
# його мовчки не застосовує, але відмову EPERM ловимо тут, а не о 01:10).
if probe -p CPUWeight=1000 -p IOSchedulingClass=best-effort -p IOSchedulingPriority=0 \
   && probe -p Nice=15 -p CPUWeight=20 -p IOSchedulingClass=best-effort \
            -p IOSchedulingPriority=7 -p OOMScoreAdjust=500 \
   && probe -p Nice=10 -p CPUWeight=20 -p IOSchedulingClass=best-effort \
            -p IOSchedulingPriority=7 -p OOMScoreAdjust=500 \
            -p MemoryHigh=700M -p MemoryMax=1000M \
   && probe -p Nice=15 -p CPUWeight=20 -p IOWeight=20 -p IOSchedulingClass=best-effort \
            -p IOSchedulingPriority=7 -p OOMScoreAdjust=500 \
            -p MemoryHigh=1800M -p MemoryMax=2600M; then
  echo "   ok"
else
  echo "   ПОМИЛКА: systemd не прийняв налаштувань пріоритету — юніти НЕ оновлено." >&2
  echo "   Див. implementation-notes.md, D50 (пріоритети; що потребує root — дія власника)." >&2
  exit 1
fi

echo "== юніти systemd (користувацькі)"
mkdir -p "$UNITS"
# realty-identity.timer замінено на realty-night.timer (E9, D53): дозбір identity —
# одна з робіт нічного диригента. Рішення «вмикати нічний таймер» — зі стану, який
# переживає обірваний запуск (рецензія E9, D53): нічний потрібен, якщо увімкнений
# старий identity, сам нічний (повторний запуск) або цикл (служби вже працюють).
# Порядок: спершу новий юніт і його таймер, лише потім вимкнути й прибрати старий —
# обрив посередині (Ctrl-C, обрив ssh) не лишає машину без нічного таймера.
want_night=0
for unit in realty-identity.timer realty-night.timer realty-cycle.timer; do
  if systemctl --user is-enabled --quiet "$unit" 2>/dev/null; then
    want_night=1
  fi
done
# Разом із шаблоном realty-lookup@.service (перевірка при відкритті квартири).
cp deploy/fedora/systemd/*.service deploy/fedora/systemd/*.timer "$UNITS/"
systemctl --user daemon-reload
if [ "$want_night" = 1 ]; then
  systemctl --user enable --now realty-night.timer
  echo "   нічний диригент: realty-night.timer (01:10 і 04:10)"
fi
# Старий таймер і служба: --now на таймері не зупиняє служби, що вже йде (розгортання
# о 01:10–03:00), — її зупиняємо окремо, щоб вона не тримала замок без юніта.
systemctl --user disable --now realty-identity.timer 2>/dev/null || true
systemctl --user stop realty-identity.service 2>/dev/null || true
rm -f "$UNITS/realty-identity.service" "$UNITS/realty-identity.timer"
systemctl --user daemon-reload

if [ "${1:-}" = "--enable" ]; then
  if [ -f data/COLLECTOR_OFF ]; then
    echo "УВАГА: data/COLLECTOR_OFF існує — збір на цій машині вимкнено свідомо." >&2
    exit 1
  fi
  systemctl --user enable --now realty-web.service realty-tunnel.service \
    realty-cycle.timer realty-backup.timer realty-watchdog.timer realty-night.timer realty-dedup-sample.timer
  systemctl --user list-timers 'realty-*' --no-pager
fi
echo "Готово."
