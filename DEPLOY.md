# Розгортання 24/7

## Fedora-ноутбук як сервер (поточний варіант)

Усе керується користувацькими службами systemd (`systemctl --user`), без sudo.
Єдиний крок із sudo — одноразовий `deploy/fedora/root-setup.sh`.

| служба / таймер | що робить |
|---|---|
| `realty-web.service` | веб на `127.0.0.1:8000`, перезапуск після падіння |
| `realty-tunnel.service` | Cloudflare Tunnel назовні; без `AUTH_*` не стартує |
| `realty-cycle.timer` | цикл кожні 3 год (00:05, 03:05…); пропущений — після старту |
| `realty-backup.timer` | бекап о 04:30 з перевіркою відновлення й копією поза машиною |
| `realty-watchdog.timer` | сторож кожні 30 хв: тиша, падіння джерел, блокування, бекап |
| `realty-night.timer` | нічний диригент 01:10 і 04:10 (`cli.py night`, D53): бекап на старті, перевірка актуальності M2/M3 смугами хостів, дозбір identity; замість `realty-identity.timer` |
| `realty-lookup@.service` | шаблон: перевірка квартири, яку щойно відкрили (запускає сайт) |

Порядок першого розгортання:

```bash
sudo bash ~/realty/deploy/fedora/root-setup.sh     # кришка, сон, linger — один раз
bash ~/realty/deploy/fedora/install.sh             # залежності, Chromium, cloudflared, юніти
bash deploy/migrate-to-fedora.sh u@<fedora>        # на MacBook: вимкнути збір там, перенести базу
bash ~/realty/deploy/fedora/install.sh --enable    # увімкнути служби
```

Оновлення коду (між циклами; кроки циклу запускають `cli.py` з диска, тож
`git pull` посеред циклу змішав би версії коду):

```bash
systemctl --user list-timers 'realty-*'            # цикл і нічні роботи неактивні, ≥30 хв до старту
.venv/bin/python cli.py backup run                 # свіжий бекап із перевіркою відновлення
git pull && .venv/bin/python cli.py config check   # ненульовий код — зупинитись
.venv/bin/python cli.py db migrate --dry-run       # план змін схеми (лише читання)
.venv/bin/python cli.py db plans --preview         # плани запитів сайту на копії з індексами
.venv/bin/python cli.py db migrate                 # під замком циклу, одна транзакція; розбіжність — ROLLBACK
bash deploy/fedora/install.sh                      # юніти (пробний запуск пріоритетів), daemon-reload
systemctl --user restart realty-web                # нові кеші, стиснення, черга перевірок
.venv/bin/python cli.py speed priorities           # чи діють пріоритети (cgroup, nice, ionice)
```

Райони й ЖК (Блок 4, крок E10, D57) — після `db migrate` (схема S4: 14 колонок, індекс
`ix_listings_place`; ops.db — `places_runs`, `dedup_audits.suspicious_core`: їх додає
init_ops() першого ж процесу, `db migrate --dry-run` їх показує), між циклами. Без
`db migrate` команди `places` відмовляють («спершу `cli.py db migrate`», код 2) і схеми
не чіпають:

```bash
.venv/bin/python cli.py places check               # довідник config/places/ без колізій
.venv/bin/python cli.py places assign --dry-run    # охоплення до/після, нерозпізнані назви, would_change = 0
.venv/bin/python cli.py places sample --out ~/places_sample.csv   # вибірка + докази будинку
```

**СТОП: власник переглядає вибірку** (≥50 на джерело й ступінь і шар «громада»; ціль —
район ≥90%, ЖК ≥98%; колонки ria_district_same_addr / lun_label_same_addr /
zhk_same_addr — що кажуть інші квартири за тією самою адресою). До його «так» крок у
циклі вимкнено (`config/cycle.toml` `[places] enabled = false` — диригент його не ставить),
а сайт показує сирий район, як до E10 (без фільтрів місця). Після «так»:

```bash
.venv/bin/python cli.py backup run                 # бекап перед першим записом ключів
.venv/bin/python cli.py places assign              # під замком циклу; лише порожні ключі
.venv/bin/python cli.py dedup                      # квартири й row_* (або дочекатися кроку «дублі»)
# config/cycle.toml: [places] enabled = true       — далі крок у кожному циклі перед «дублі»
systemctl --user restart realty-web                # фільтри «Район»/«ЖК», вкладка «Райони й ЖК»
```

Виправити ВЖЕ визначені ключі (would_change на /status, рішення власника):
`cli.py places reassign` (пробний: що змінилось би), потім `cli.py backup run` і одразу
`cli.py places reassign --apply [--ids-out ФАЙЛ]` (відмова без бекапу, свіжішого за
`rules.reassign.backup_max_age_min`; id змінених — у ops.places_runs).

**Швидкість — умова приймання.** Після перезапуску realty-web: `cli.py speed probe` (або
дочекатися зондів) і через добу `cli.py speed report` — p95 «/» за 24 год проти D54. Якщо
p95 «/» виріс більш ніж на 50 мс або «Райони й ЖК» (/places) p95 > 300 мс — відкат
(`git checkout <попередній коміт>` + перезапуск realty-web; нові колонки й індекс старий
код ігнорує) або ескалація з числами.

Сирі поля (district, complex_name, location, title, last_seen) крок не змінює ніколи;
відкат — попередній коміт (нові колонки старий код ігнорує; DROP INDEX
ix_listings_place — за бажанням). flombu після E10 збирає ~400 квартир замість 27
(перший цикл — ще ~380 сторінок оголошень, ≈7 хв); населений пункт поза містом і
довідником (Тисмениця) відкидається, точність точки — за рівнем геокодування (лише з
номером будинку — «точна»).

`install.sh` вмикає `realty-night.timer`, якщо увімкнений старий `realty-identity.timer`,
сам нічний або `realty-cycle.timer`, і лише ПІСЛЯ цього вимикає й прибирає старий
таймер (і зупиняє його службу, якщо та саме йде): обірваний запуск можна просто
повторити — машина не лишиться без нічного таймера.

Відкат нічного диригента (E9, D53) на попередній коміт:

```bash
systemctl --user disable --now realty-night.timer
systemctl --user stop realty-night.service 2>/dev/null || true
rm -f ~/.config/systemd/user/realty-night.service ~/.config/systemd/user/realty-night.timer
git checkout <попередній-коміт>
bash deploy/fedora/install.sh                      # старий install.sh поверне realty-identity.*
systemctl --user enable --now realty-identity.timer
systemctl --user list-timers 'realty-*'            # realty-identity є, realty-night немає
```

Ранкова звірка після нічного вікна (`cli.py liveness report --last 2`): актуальних
після = до + повернуто − знято + нові оголошення дозбору identity; подій ціни —
лише від дозбору identity. З E11 (D60) прохід стрічки LUN/flombu — уже НЕ збір: нових
оголошень, подій ціни й last_seen він не пише, тож обидва доданки — 0, і будь-яка
різниця — «НЕЗВІРЕНО».

Нічні роботи доказів Блоків 3/4 (E11, D60) — розгортання між циклами, як вище:
`config check` (нова тема `seller`), `db migrate` (realty.db — без змін: колонки
seller_evidence/seller_profile/place_raw є з S3; ops.db — `night_state`, `olx_tab_seen`,
`night_runs.evidence`: їх додає init_ops(), `db migrate --dry-run` показує), юніти не
змінюються (Chromium для смуги рендерів — той самий, що й для збору OLX; MemoryMax
нічного юніта 2600M). Перевірка: `cli.py night --dry-run` — розділ «ДОЗБІР ДОКАЗІВ»
(рендери OLX, стрічка LUN/flombu, GET rieltor замість HEAD); уранці — `cli.py liveness
report --last 2` (рядки «дозбір доказів», «прохід стрічки», «мітки вкладок OLX»),
`GET /api/status/night` (покриття доказами за групами). Відкат — попередній коміт (нові
таблиці й колонку ops.db старий код ігнорує; докази, вже дописані в seller_evidence і
place_raw, — лише нові ключі JSON, старий код їх не читає).

Щоденне:

```bash
systemctl --user list-timers 'realty-*'            # коли наступні запуски
journalctl --user -u realty-cycle -n 50            # що було в останньому циклі
journalctl --user -u realty-night -n 80            # останнє нічне вікно (бекап, смуги, пакети)
.venv/bin/python cli.py liveness report --last 2   # дві останні нічні вікна по сайтах
.venv/bin/python cli.py night --dry-run            # план наступного вікна: ключі × крок, дозбір доказів
cat ~/realty/data/public_url                       # поточна адреса ззовні
python cli.py watchdog --test                      # перевірити канал Telegram
```

### Бекап у хмару

Копія поза машиною може йти в Telegram (просто, але бот не надсилає файли
більші за 50 МБ) і/або в хмару через rclone — це основний шлях, коли база
виросте. Доступ до акаунта дає людина одноразово, на самій машині:

```bash
rclone config create gdrive drive scope=drive.file
```

Відкриється браузер із запитом дозволу. `scope=drive.file` означає, що rclone
бачить лише ті файли, які сам і створив, — решта диска йому недоступна. Далі
в `.env`:

```
BACKUP_RCLONE_REMOTE=gdrive:realty-backups
BACKUP_TELEGRAM=0
```

Бекап зараховується успішним, лише якщо архів долетів і його розмір на тому
боці збігся з локальним. У хмарі лишаються `BACKUP_KEEP` останніх архівів.

**Що треба буде зробити протягом 2026 року.** rclone ходить у Google зі спільним
ключем застосунку, який Google вимикає. Коли це станеться, вивантаження почне
падати (сторож повідомить у той самий день). Лікується створенням власного
client_id: rclone.org/drive/#making-your-own-client-id — безкоштовно, робиться
в акаунті власника.

### Домен

Поки домену немає, тунель тимчасовий (`*.trycloudflare.com`): адреса міняється
після кожного перезапуску і приходить у Telegram. Коли домен з'явиться:
створити іменований тунель у Cloudflare, вписати в `.env` два значення й
перезапустити одну службу — більше нічого не міняється.

```
PUBLIC_DOMAIN=realty.приклад.ua
CLOUDFLARE_TUNNEL_TOKEN=…
```

```bash
systemctl --user restart realty-tunnel
```

## Контейнер (Docker, запасний варіант)


Мета: система доступна за постійним посиланням, працює при вимкненому
ноутбуці, база не зникає при редеплої.

## Що потрібно від сервера

| | мінімум | чому саме так |
|---|---|---|
| RAM | 2 ГБ | Chromium для OLX з'їдає до 1 ГБ під час рендера |
| Диск | 20 ГБ **постійний** | образ Playwright ~2 ГБ, база зараз 55 МБ і росте |
| CPU | 1–2 ядра | збір іде порціями, пікового навантаження немає |

Ефемерний диск не годиться: разом із базою зникне вся історія цін.

## Змінні оточення

Задаються на боці хостингу, **не в репозиторії**:

```
AUTH_USER=           # логін до панелі
AUTH_PASSWORD=       # пароль
ANTHROPIC_API_KEY=   # для LLM-фолбеку, необов'язково
DB_URL=sqlite:////data/realty.db
OPS_DB_URL=sqlite:////data/ops.db
HOST=0.0.0.0
```

Без `AUTH_USER`/`AUTH_PASSWORD` інтерфейс відкритий — для публічного сервера
це обов'язкові змінні.

## Запуск

```bash
git clone git@github.com:Vasia228-web/Monitoring-CRM.git && cd Monitoring-CRM
cp .env.example .env    # заповнити AUTH_* і ключ
docker compose up -d --build
```

`restart: unless-stopped` піднімає обидва сервіси після падіння й після
перезавантаження сервера. Веб слухає `:8000`, воркер збирає кожні 3 години.

## Перенесення наявної бази

```bash
scp data/realty.db data/ops.db  сервер:/шлях/до/тому/
```

Або лишити порожню: перший повний збір відновить дані за ~4 години.

## Перевірка після деплою

```bash
curl -u LOGIN:PASS https://адреса/healthz      # {"ok": true}
curl -I https://адреса/                         # без пароля має бути 401
curl -u LOGIN:PASS https://адреса/api/status    # стан воркера
```

Ознака, що фон працює без ноутбука: у `/status` час останнього прогону
оновлюється, а `Додано` росте.
