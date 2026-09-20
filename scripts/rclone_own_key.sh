#!/bin/bash
# Підстановка ВЛАСНОГО ключа Google замість спільного ключа rclone.
#
# Навіщо: rclone ходить у Google зі спільним ключем застосунку, який Google
# вимикає протягом 2026 року. Коли це станеться, вивантаження бекапів почне
# падати (сторож напише того ж дня, але копії поза машиною не буде).
#
# Ключ створюється в АКАУНТІ ВЛАСНИКА — інструкція в кінці файлу.
# Далі просто:
#
#     bash ~/realty/scripts/rclone_own_key.sh
#
# Скрипт спитає два значення, підставить їх, попросить Google видати новий
# дозвіл і одразу прожене справжній бекап — щоб переконатись, що все працює.
set -euo pipefail
cd "$(dirname "$0")/.."
RCLONE="${RCLONE_BIN:-$HOME/.local/bin/rclone}"
REMOTE="${1:-gdrive}"

[ -x "$RCLONE" ] || { echo "rclone не знайдено: $RCLONE" >&2; exit 1; }
"$RCLONE" listremotes | grep -qx "$REMOTE:" || {
  echo "сховища '$REMOTE:' немає; є такі: $($RCLONE listremotes | tr '\n' ' ')" >&2; exit 1; }

echo "Значення беруться з Google Cloud → Credentials → OAuth client (Desktop app)."
read -rp "client_id:     " CLIENT_ID
read -rsp "client_secret: " CLIENT_SECRET; echo
[ -n "$CLIENT_ID" ] && [ -n "$CLIENT_SECRET" ] || { echo "порожнє значення" >&2; exit 1; }

# Копія налаштувань rclone — щоб було куди повернутись.
cp -a "$HOME/.config/rclone/rclone.conf" "$HOME/.config/rclone/rclone.conf.bak.$(date +%Y%m%d-%H%M%S)"

"$RCLONE" config update "$REMOTE" client_id "$CLIENT_ID" client_secret "$CLIENT_SECRET" \
  --non-interactive >/dev/null
echo "ключ підставлено; тепер Google попросить видати дозвіл заново"

# Повторний дозвіл: старий токен виданий на спільний ключ і з новим не працює.
"$RCLONE" config reconnect "$REMOTE": || {
  echo "дозвіл не видано — налаштування лишились зі старим ключем;" >&2
  echo "повернути попередній стан: cp ~/.config/rclone/rclone.conf.bak.* ~/.config/rclone/rclone.conf" >&2
  exit 1; }

echo "--- перевірка доступу"
"$RCLONE" lsd "$REMOTE": >/dev/null && echo "сховище відповідає"

echo "--- справжній бекап через новий ключ"
set -a; . ./.env; set +a
.venv/bin/python cli.py backup

cat <<'NOTE'

Готово. Якщо в цьому виводі немає попередження про спільний client_id —
ключ уже власний.

--- Як отримати ці два значення (робить власник акаунта, ~5 хвилин) ---
1. console.cloud.google.com → створити проєкт (назва будь-яка).
2. APIs & Services → Library → знайти "Google Drive API" → Enable.
3. APIs & Services → OAuth consent screen → тип External → заповнити назву
   застосунку й свою пошту → Save. У розділі Audience → Test users → додати
   свою ж адресу Gmail.
4. APIs & Services → Credentials → Create credentials → OAuth client ID →
   Application type: Desktop app → Create.
5. Скопіювати Client ID і Client secret — це і є два значення вище.
NOTE
