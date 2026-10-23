# Переезд на боевой сервер — что нужно от администратора

Документ для системного администратора, который предоставит сервер. Внутри — что нужно
предоставить, что настроить и как проверить, что всё живо. Секретов здесь нет: пароли и
ключи передаются отдельно.

## Что за проект

- Сайт-афиша событий Новокузнецка: **FastAPI (Python 3.11) + SQLite + статический фронт**,
  фронт отдаёт тот же процесс uvicorn (один origin, никакой сборки JS нет).
- Приложение — ASGI-процесс под systemd, наружу смотрит nginx с TLS. Ни PHP, ни MySQL,
  ни Node, ни Docker не нужны.
- Персональные данные пользователей (имя, e-mail) → сервер должен быть **в РФ** (152-ФЗ).
- Репозиторий: `https://github.com/Wundrwaffle/Web-Afisha-Kaktus` (публичный, read-only).

## Что нужно предоставить (доступы и разрешения)

| Что | Зачем |
|---|---|
| VPS: 2 vCPU / 2 ГБ RAM / 20 ГБ SSD, Ubuntu 22.04 или 24.04 LTS (Debian 12 тоже ок) | Узкое место — не CPU, а память под uvicorn + nginx. Хватит с запасом на годы |
| root или пользователь с `sudo` по **SSH-ключу** | Установка пакетов, systemd, nginx, выпуск сертификата |
| Право создавать systemd-сервис и системного пользователя `afisha` | Приложение работает отдельным юзером, не под root |
| Пакеты: `python3.11` (+`venv`), `nginx`, `certbot` + `python3-certbot-nginx`, `sqlite3`, `git`, `cron` | Всё из штатных репозиториев |
| Firewall: открыты **22** (по ключу), **80**, **443**. Порт приложения **8000 наружу НЕ открывать** | Uvicorn слушает только `127.0.0.1` |
| Доступ к DNS домена `чтоунастамзавтра.рф` (панель reg.ru) — либо вы вносите записи по нашей просьбе | Домен куплен на reg.ru, NS `ns1/ns2.reg.ru`, сейчас стоит **парковка** |
| Выпуск TLS на **punycode**-имена: `xn--80aaaaj2a8afi1afieff5d.xn--p1ai` и `www.xn--80aaaaj2a8afi1afieff5d.xn--p1ai` | ACME с IDN работает, но в конфигах и утилитах — только punycode |
| Часовой пояс сервера: **Asia/Novokuznetsk** (UTC+7) | Приложение считает `date.today()` локально: от TZ зависят «предстоящие события» и напоминания «за день» |
| Исходящая сеть: `pypi.org` на время установки зависимостей | Больше ничего не требуется |
| Ежедневный бэкап БД (см. ниже) + хранение 14 дней | SQLite — единственный носитель данных |

## Что настроить

### 1. Пользователь, код, зависимости

```bash
useradd --system --create-home --home-dir /opt/afisha --shell /usr/sbin/nologin afisha
install -d -o afisha -g afisha -m 755 /opt/afisha
install -d -o afisha -g afisha -m 750 /var/lib/afisha        # здесь будет БД
install -d -o root   -g afisha -m 750 /etc/afisha            # здесь env-файл

sudo -u afisha git clone https://github.com/Wundrwaffle/Web-Afisha-Kaktus.git /opt/afisha/app
sudo -u afisha python3.11 -m venv /opt/afisha/venv
sudo -u afisha /opt/afisha/venv/bin/pip install -U pip
sudo -u afisha /opt/afisha/venv/bin/pip install -r /opt/afisha/app/backend/requirements.txt
```

Сборки «с нуля» не нужны: все зависимости ставятся колёсами (bcrypt, cryptography, uvloop),
компилятор на сервере не требуется.

### 2. Конфигурация приложения — `/etc/afisha/afisha.env`

Владелец `root:afisha`, права `640`. Значения секретов придут отдельным сообщением.

```ini
AFISHA_DATABASE_URL=sqlite:////var/lib/afisha/afisha.sqlite3
AFISHA_SECRET_KEY=<48+ случайных символов, выдаём отдельно>
AFISHA_PUBLIC_MODE=1
AFISHA_SERVE_STATIC=1
```

Три вещи, которые легко сделать неправильно:

- **`AFISHA_GATE_USER` / `AFISHA_GATE_PASSWORD` не задавать.** Basic-гейт — это режим
  показа «по ссылке», в проде он должен быть выключен, иначе сайт закрыт от всех.
- `AFISHA_SECRET_KEY` **не менять** после запуска: при смене разом инвалидируются все
  сессии и refresh-токены (пользователей разлогинит).
- Путь к БД — с **четырьмя** слэшами (`sqlite:////var/lib/...`): три слэша означают
  относительный путь и файл уедет в рабочий каталог сервиса.

### 3. systemd — `/etc/systemd/system/afisha.service`

```ini
[Unit]
Description=Afisha Kaktus (FastAPI/uvicorn)
After=network.target

[Service]
Type=simple
User=afisha
Group=afisha
WorkingDirectory=/opt/afisha/app/backend
EnvironmentFile=/etc/afisha/afisha.env
Environment=TZ=Asia/Novokuznetsk
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/afisha/venv/bin/uvicorn app.main:app \
    --host 127.0.0.1 --port 8000 --workers 1 \
    --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

- **`--workers 1` обязателен.** Приложение хранит лимиты на подбор пароля в памяти
  процесса и работает с SQLite; несколько воркеров ослабят лимиты и дадут блокировки БД.
- `--proxy-headers --forwarded-allow-ips=127.0.0.1` — чтобы приложение доверяло заголовкам
  от nginx и понимало, что запрос пришёл по HTTPS.

```bash
systemctl daemon-reload && systemctl enable --now afisha.service
```

### 4. nginx — `/etc/nginx/sites-available/afisha`

```nginx
server {
    listen 80;
    listen [::]:80;
    server_name xn--80aaaaj2a8afi1afieff5d.xn--p1ai www.xn--80aaaaj2a8afi1afieff5d.xn--p1ai;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name xn--80aaaaj2a8afi1afieff5d.xn--p1ai www.xn--80aaaaj2a8afi1afieff5d.xn--p1ai;

    # сертификат — после того, как A-запись начнёт отвечать этим сервером
    # certbot --nginx -d xn--80aaaaj2a8afi1afieff5d.xn--p1ai -d www.xn--80aaaaj2a8afi1afieff5d.xn--p1ai

    client_max_body_size 1m;
    gzip on;
    gzip_types text/html text/css application/javascript application/json image/svg+xml;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        # ОБЯЗАТЕЛЬНО: по этому заголовку приложение решает, отдавать ли HSTS
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # статика и юр-документы меняются редко — можно кэшировать на клиенте
    location ~ ^/(brand|legal)/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        expires 30d;
        add_header Cache-Control "public, immutable";
    }
}
```

**Проксируется весь сайт целиком, включая статику.** Фронт отдаёт тот же uvicorn:
это даёт один origin, отсутствие CORS и рабочие короткие адреса юр-документов
(`/terms`, `/privacy`, `/cookies`, `/recommendations` — PDF, `/offer` → 301 на `/terms`).
Если отдавать `docs/` напрямую из nginx, эти адреса начнут отдавать 404.

### 5. DNS (панель reg.ru)

- Снять парковку, поставить A-записи: `@` → IP сервера, `www` → IP сервера.
- NS остаются `ns1.reg.ru` / `ns2.reg.ru` — переводить домен на другие NS не нужно.
- TTL на время переезда — 300, потом можно вернуть.
- Сертификат выпускать **после** того, как A-запись уже указывает на сервер.

### 6. Бэкап БД — `/etc/cron.d/afisha-backup`

```cron
15 4 * * * afisha /usr/bin/sqlite3 /var/lib/afisha/afisha.sqlite3 ".backup '/var/backups/afisha/afisha-$(date +\%F).sqlite3'" && find /var/backups/afisha -name 'afisha-*.sqlite3' -mtime +14 -delete
```

`sqlite3 .backup` (а не `cp`) — единственный способ получить целую копию на живой базе.
Каталог создаётся один раз: `install -d -o afisha -g afisha -m 700 /var/backups/afisha`. Хорошо
бы дополнительно отправлять копию в отдельное хранилище (S3/внешний диск), чтобы бэкап
не лежал на том же сервере.

### 7. Логи

Достаточно journald: `journalctl -u afisha -f`. Ротацию journald делать не нужно, если
задан `SystemMaxUse` (например 500M) в `/etc/systemd/journald.conf`.

## Что делаем мы

- Присылаем значения `AFISHA_SECRET_KEY` отдельным защищённым каналом.
- После первого запуска наполняем БД (события, аккаунты) — отдельными командами.
- Передаём короткую инструкцию деплоя: `git pull` → `pip install -r requirements.txt` →
  `systemctl restart afisha` (сделаем скриптом `deploy.sh` в репозитории).
- Дальше работаем сами через SSH: правки кода, миграции, наполнение.

## Приёмка — что должно отвечать после переезда

```bash
curl -s  https://xn--80aaaaj2a8afi1afieff5d.xn--p1ai/api/v1/health          # {"status":"ok"}
curl -sI https://xn--80aaaaj2a8afi1afieff5d.xn--p1ai/ | head -1             # HTTP/2 200
curl -sI https://xn--80aaaaj2a8afi1afieff5d.xn--p1ai/ | grep -i strict-transport  # HSTS есть
curl -sI https://xn--80aaaaj2a8afi1afieff5d.xn--p1ai/terms | grep -i content-type # application/pdf
curl -sI https://xn--80aaaaj2a8afi1afieff5d.xn--p1ai/offer | head -1        # 301 → /terms
curl -s  -o /dev/null -w '%{http_code}\n' https://xn--80aaaaj2a8afi1afieff5d.xn--p1ai/openapi.json   # 404
curl -s  -o /dev/null -w '%{http_code}\n' https://xn--80aaaaj2a8afi1afieff5d.xn--p1ai/api/v1/me/favorites # 401
curl -sI http://xn--80aaaaj2a8afi1afieff5d.xn--p1ai/ | head -1              # 301 → https
```

Плюс в браузере: открывается главная, работает регистрация/вход, вход организатора,
кириллица в поиске находит события. `journalctl -u afisha` без ошибок. Бэкап-файл за
прошедшую ночь на месте.

## Чего НЕ надо (чтобы не потратить время)

- **Шаред-хостинг с панелью и «поддержкой Python»** — там обычно только WSGI/PHP-модуль,
  ASGI-процесс (uvicorn) не живёт. Нужен VPS.
- MySQL/PostgreSQL, Redis, Docker, Node.js — не используются. Ставить не нужно.
- FTP/SFTP-доступ к файлам сайта и правка файлов «на сервере руками» — деплой только из git.
- Открытый наружу порт 8000, панель управления на публичном порту, доступ root по паролю.
