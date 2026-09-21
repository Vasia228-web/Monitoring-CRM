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
CLIENT_ID="$(echo -n "$CLIENT_ID" | tr -d '[:space:]')"
CLIENT_SECRET="$(echo -n "$CLIENT_SECRET" | tr -d '[:space:]')"

# Перевірки формату ДО того, як щось міняти. 21.09.2026 секрет вставився двічі
# (70 символів, «GOCSPX-» двічі) — Google відповідав invalid_client.
case "$CLIENT_ID" in
  *.apps.googleusercontent.com) ;;
  *) echo "client_id має закінчуватись на .apps.googleusercontent.com" >&2; exit 1 ;;
esac
[ "$(grep -o 'apps.googleusercontent.com' <<<"$CLIENT_ID" | wc -l)" -eq 1 ] || {
  echo "client_id схоже вставлено двічі — вставте один раз" >&2; exit 1; }
case "$CLIENT_SECRET" in
  GOCSPX-*) ;;
  *) echo "client_secret має починатись з GOCSPX-" >&2; exit 1 ;;
esac
[ "$(grep -o 'GOCSPX-' <<<"$CLIENT_SECRET" | wc -l)" -eq 1 ] || {
  echo "client_secret схоже вставлено двічі (GOCSPX- трапляється більше одного разу)" >&2; exit 1; }

CONF="$HOME/.config/rclone/rclone.conf"
BACKUP="$CONF.bak.$(date +%Y%m%d-%H%M%S)"
cp -a "$CONF" "$BACKUP"
# Будь-яка невдача далі — повертаємо робочі налаштування самі, не людина.
restore() {
  cp -a "$BACKUP" "$CONF"
  echo "НЕ ВДАЛОСЯ — налаштування rclone повернуто як було (копія: $BACKUP)" >&2
}
trap 'restore' ERR

token_before="$("$RCLONE" config show "$REMOTE" | grep '^token' | sha256sum)"
"$RCLONE" config update "$REMOTE" client_id "$CLIENT_ID" client_secret "$CLIENT_SECRET" \
  --non-interactive >/dev/null
echo "ключ підставлено; тепер Google попросить видати дозвіл заново"
echo "(відкриється браузер — кришка ноутбука має бути відкрита)"

# Повторний дозвіл: старий токен виданий на спільний ключ і з новим не працює.
"$RCLONE" config reconnect "$REMOTE":
token_after="$("$RCLONE" config show "$REMOTE" | grep '^token' | sha256sum)"
if [ "$token_before" = "$token_after" ]; then
  echo "Google не видав нового токена — дозвіл не завершено" >&2
  false
fi
echo "новий токен отримано"

echo "--- перевірка доступу"
"$RCLONE" lsd "$REMOTE": >/dev/null
echo "сховище відповідає"
trap - ERR

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
