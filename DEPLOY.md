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

Порядок першого розгортання:

```bash
sudo bash ~/realty/deploy/fedora/root-setup.sh     # кришка, сон, linger — один раз
bash ~/realty/deploy/fedora/install.sh             # залежності, Chromium, cloudflared, юніти
bash deploy/migrate-to-fedora.sh u@<fedora>        # на MacBook: вимкнути збір там, перенести базу
bash ~/realty/deploy/fedora/install.sh --enable    # увімкнути служби
```

Щоденне:

```bash
systemctl --user list-timers 'realty-*'            # коли наступні запуски
journalctl --user -u realty-cycle -n 50            # що було в останньому циклі
cat ~/realty/data/public_url                       # поточна адреса ззовні
python cli.py watchdog --test                      # перевірити канал Telegram
```

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
