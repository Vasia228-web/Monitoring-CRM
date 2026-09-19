#!/bin/bash
# Перенесення бази MacBook → Fedora. Запускається на MacBook.
#
#     bash deploy/migrate-to-fedora.sh u@192.168.0.128
#
# Правило: дві машини НІКОЛИ не збирають одночасно — інакше історія цін
# розійдеться на дві версії. Тому порядок жорсткий:
#   1. вимкнути збір на MacBook (прапорець + вивантаження агента launchd)
#      і дочекатися, поки поточний цикл закінчиться;
#   2. свіжа узгоджена копія через SQLite backup API (не cp) з маніфестом;
#   3. перенести архів, на Fedora перевірити цілісність і кількість рядків;
#   4. розгорнути в ~/realty/data лише якщо там ще НЕМАЄ бази (не перезаписуємо);
#   5. кількість записів до і після має збігтися — інакше зупинка.
# MacBook не чиститься: база лишається там недоторканою.
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET="${1:?вкажіть user@host Fedora}"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 "$TARGET")

echo "== 1. Вимикаю збір на MacBook"
# Спершу прапорець: новий цикл його побачить і не почнеться. Агент launchd
# вивантажуємо лише ПІСЛЯ того, як поточний цикл закінчиться, — bootout
# убив би його посеред кроку.
echo "базу перенесено на Fedora ($TARGET) $(date '+%Y-%m-%d %H:%M'); збір тут вимкнено свідомо" \
  > data/COLLECTOR_OFF
# Чекаємо, поки поточний цикл відпустить замок (не довше за стелю циклу).
.venv/bin/python - <<'PY'
import fcntl, time, sys
deadline = time.time() + 2.5 * 3600
with open("data/cycle.lock", "a+") as fh:
    while True:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.time() > deadline:
                sys.exit("цикл не закінчився за 2,5 год — зупинка")
            print("   чекаю завершення поточного циклу…", flush=True)
            time.sleep(30)
print("   збір на MacBook зупинено")
PY
launchctl bootout "gui/$(id -u)/com.yavasia.realty.incremental" 2>/dev/null || true

echo "== 2. Свіжа копія"
.venv/bin/python cli.py backup --no-upload
ARCHIVE="$(ls -1t data/backups/realty-backup-*.tar.xz | head -1)"
MAC_ROWS="$(.venv/bin/python -c "
import json,sys; from realty import backup
print(json.dumps(backup.verify_archive(__import__('pathlib').Path('$ARCHIVE'))['rows'], sort_keys=True))")"
echo "   архів: $ARCHIVE"
echo "   рядків на MacBook: $MAC_ROWS"

echo "== 3. Перенесення"
"${SSH[@]}" "mkdir -p ~/realty-transfer"
scp -q -o BatchMode=yes "$ARCHIVE" "$TARGET:realty-transfer/"
for f in data/quality_thresholds.json data/analytics_settings.json; do
  [ -f "$f" ] && scp -q -o BatchMode=yes "$f" "$TARGET:realty-transfer/"
done
[ -d data/snapshots ] && scp -q -r -o BatchMode=yes data/snapshots "$TARGET:realty-transfer/"

echo "== 4–5. Перевірка й розгортання на Fedora"
NAME="$(basename "$ARCHIVE")"
FEDORA_ROWS="$("${SSH[@]}" "cd ~/realty && .venv/bin/python - <<PY
import json, pathlib, shutil, tarfile, tempfile
from realty import backup
arch = pathlib.Path.home() / 'realty-transfer' / '$NAME'
data = pathlib.Path('data')
for name in ('realty.db', 'ops.db'):
    if (data / name).exists():
        raise SystemExit(f'data/{name} уже існує на Fedora — не перезаписую')
check = backup.verify_archive(arch)
if not check['match']:
    raise SystemExit(f'архів не пройшов перевірку: {check[\"integrity\"]}')
with tempfile.TemporaryDirectory() as tmp:
    tarfile.open(arch, 'r:xz').extractall(tmp, filter='data')
    for name in ('realty.db', 'ops.db'):
        if (pathlib.Path(tmp) / name).exists():
            shutil.move(pathlib.Path(tmp) / name, data / name)
src = pathlib.Path.home() / 'realty-transfer'
for f in ('quality_thresholds.json', 'analytics_settings.json'):
    if (src / f).exists() and not (data / f).exists():
        shutil.copy2(src / f, data / f)
if (src / 'snapshots').exists() and not (data / 'snapshots').exists():
    shutil.copytree(src / 'snapshots', data / 'snapshots')
assert backup.integrity(data / 'realty.db') == 'ok'
print(json.dumps(backup.row_counts(data / 'realty.db'), sort_keys=True))
PY")"
echo "   рядків на Fedora:  $FEDORA_ROWS"
if [ "$MAC_ROWS" != "$FEDORA_ROWS" ]; then
  echo "НЕ ЗБІГАЄТЬСЯ — зупинка. Збір на MacBook вимкнено, на Fedora не вмикайте." >&2
  exit 1
fi
echo "Збігається. Далі на Fedora: bash ~/realty/deploy/fedora/install.sh --enable"
