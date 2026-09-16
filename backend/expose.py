"""Публичный показ афиши: поднимает backend и туннель наружу одной командой.

Зачем отдельный скрипт, а не start.bat: при показе наружу нужны три вещи,
которые нельзя забыть, иначе демо превращается в дыру —
  1) гейт по логину/паролю на весь сайт,
  2) копия базы (чужой человек не должен править рабочие данные),
  3) постоянный секрет подписи токенов.

Что делает:
  * читает/создаёт .secrets/expose.env (секреты и пароли приглашения),
  * делает копию рабочей базы в .secrets/public-afisha.sqlite3,
  * создаёт демо-аккаунты организатора и модератора в этой копии,
  * поднимает uvicorn с публичными флагами (гейт, статика, скрытый Swagger),
  * поднимает cloudflared quick tunnel, парсит публичный URL,
  * проверяет снаружи: без логина — 401, с логином — 200,
  * печатает готовое приглашение и кладёт его в .secrets/invite.txt.

Остановка — Ctrl+C: оба процесса глушатся.

Запуск:  python backend/expose.py            (или expose.bat двойным щелчком)
Опции:   --no-tunnel (только локально/LAN), --port 8000, --fresh-db,
         --lan (слушать 0.0.0.0 и показать адрес в сети)
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
SECRETS_DIR = PROJECT_ROOT / ".secrets"
ENV_FILE = SECRETS_DIR / "expose.env"
INVITE_FILE = SECRETS_DIR / "invite.txt"
PUBLIC_DB = SECRETS_DIR / "public-afisha.sqlite3"
TOOLS_DIR = PROJECT_ROOT / ".tools"
CLOUDFLARED = TOOLS_DIR / "cloudflared.exe"
CLOUDFLARED_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
)
TUNNEL_URL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
# Без похожих друг на друга символов: пароль придётся переписать руками из чата.
PASSWORD_ALPHABET = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def generate_password(length: int = 10) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


# --- .secrets/expose.env ----------------------------------------------------


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


DEFAULT_SECRETS = {
    "AFISHA_GATE_USER": "afisha",
    "DEMO_ORGANIZER_EMAIL": "friend@example.com",
    "DEMO_ORGANIZER_NAME": "Друг (тест)",
    "DEMO_MODERATOR_EMAIL": "moderator-friend@example.com",
    "DEMO_MODERATOR_NAME": "Модератор (тест)",
}


def ensure_secrets() -> dict[str, str]:
    """Секреты генерируются один раз и переиспользуются.

    Иначе после каждого перезапуска менялся бы AFISHA_SECRET_KEY (все токены
    разом становятся невалидными), пароль гейта и пароли демо-аккаунтов.
    """
    values = load_env_file(ENV_FILE)
    generated = {
        "AFISHA_SECRET_KEY": lambda: secrets.token_urlsafe(48),
        "AFISHA_GATE_PASSWORD": generate_password,
        "DEMO_ORGANIZER_PASSWORD": generate_password,
        "DEMO_MODERATOR_PASSWORD": generate_password,
    }
    changed = False
    for key, factory in generated.items():
        if not values.get(key):
            values[key] = factory()
            changed = True
    for key, value in DEFAULT_SECRETS.items():
        if not values.get(key):
            values[key] = value
            changed = True
    if changed or not ENV_FILE.exists():
        SECRETS_DIR.mkdir(parents=True, exist_ok=True)
        body = "\n".join(f"{key}={values[key]}" for key in sorted(values))
        ENV_FILE.write_text(
            "# Секреты публичного показа. В репозиторий не попадает (.gitignore).\n"
            "# Удалите файл, чтобы выпустить новые пароли и инвалидировать токены.\n" + body + "\n",
            encoding="utf-8",
        )
        try:  # файл доступен только владельцу (в Windows работает как best-effort)
            ENV_FILE.chmod(0o600)
        except OSError:
            pass
    return values


# --- база и демо-аккаунты ---------------------------------------------------


def ensure_public_db(fresh: bool) -> Path:
    if PUBLIC_DB.exists() and not fresh:
        return PUBLIC_DB
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    for candidate in (PROJECT_ROOT / "afisha.sqlite3", BACKEND_DIR / "afisha.sqlite3"):
        if candidate.exists():
            shutil.copy2(candidate, PUBLIC_DB)
            print(f"  база скопирована: {candidate.name} → {PUBLIC_DB}")
            return PUBLIC_DB
    print("  рабочая база не найдена — будет создана пустая с демо-событиями")
    return PUBLIC_DB


def ensure_demo_accounts(database_url: str, secrets_map: dict[str, str]) -> list[tuple[str, str, str]]:
    """Готовит аккаунты в копии базы. Возвращает (роль, email, пароль) для приглашения.

    Копия делается с рабочей базы, а там остались dev-аккаунты (в том числе
    admin@afisha.local) с известными паролями. Публичный показ не должен их
    нести: все прочие пользователи гасятся, их refresh-токены удаляются.
    """
    sys.path.insert(0, str(BACKEND_DIR))
    from sqlalchemy import delete, select, update  # noqa: PLC0415

    from app.db import Base, build_engine, build_session_factory  # noqa: PLC0415
    from app.models import RefreshToken, User  # noqa: PLC0415
    from app.security import hash_password  # noqa: PLC0415

    engine = build_engine(database_url)
    Base.metadata.create_all(engine)
    factory = build_session_factory(engine)

    wanted = [
        (
            "organizer",
            secrets_map["DEMO_ORGANIZER_EMAIL"],
            secrets_map["DEMO_ORGANIZER_PASSWORD"],
            secrets_map["DEMO_ORGANIZER_NAME"],
        ),
        (
            "moderator",
            secrets_map["DEMO_MODERATOR_EMAIL"],
            secrets_map["DEMO_MODERATOR_PASSWORD"],
            secrets_map["DEMO_MODERATOR_NAME"],
        ),
    ]
    created: list[tuple[str, str, str]] = []
    with factory() as session:
        for role, email, password, full_name in wanted:
            user = session.scalar(select(User).where(User.email == email.lower()))
            if user is None:
                user = User(
                    email=email.lower(),
                    password_hash=hash_password(password),
                    full_name=full_name,
                    role=role,
                    is_active=True,
                )
                session.add(user)
            else:
                # Пароль из файла секретов — источник истины, иначе приглашение
                # с уже существующим аккаунтом окажется нерабочим.
                user.password_hash = hash_password(password)
                user.role = role
                user.is_active = True
            created.append((role, email, password))
        session.commit()

        # Гасим всё, что осталось от разработки: чужой человек получает только
        # демо-роли, а старые пароли в копии перестают работать.
        demo_emails = [email.lower() for _, email, _, _ in wanted]
        disabled = session.execute(
            update(User).where(User.email.notin_(demo_emails)).values(is_active=False)
        ).rowcount
        session.execute(delete(RefreshToken))
        session.commit()
    engine.dispose()
    if disabled:
        print(f"  прочие аккаунты отключены в копии: {disabled}")
    return created


# --- процессы ---------------------------------------------------------------


def wait_for_health(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/api/v1/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.5)
    return False


def ensure_cloudflared() -> Path:
    if CLOUDFLARED.exists():
        return CLOUDFLARED
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"  скачиваю cloudflared (~35 МБ) → {CLOUDFLARED}")
    with urllib.request.urlopen(CLOUDFLARED_URL, timeout=180) as response:
        CLOUDFLARED.write_bytes(response.read())
    return CLOUDFLARED


class Tunnel:
    """Обёртка над cloudflared: держит процесс и вытаскивает публичный URL."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.process: subprocess.Popen | None = None
        self.url: str | None = None
        self._lines: list[str] = []
        self._lock = threading.Lock()

    def _reader(self, stream) -> None:
        for line in stream:
            with self._lock:
                self._lines.append(line.rstrip())

    def start(self, protocol: str | None = None, timeout: float = 60.0) -> str | None:
        self._lines = []
        command = [
            str(CLOUDFLARED),
            "tunnel",
            "--url",
            f"http://127.0.0.1:{self.port}",
            "--no-autoupdate",
        ]
        if protocol:
            command += ["--protocol", protocol]
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        threading.Thread(target=self._reader, args=(self.process.stdout,), daemon=True).start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                found = next(
                    (match.group(0) for line in self._lines for match in [TUNNEL_URL_RE.search(line)] if match),
                    None,
                )
            if found:
                self.url = found
                return found
            if self.process.poll() is not None:
                return None
            time.sleep(0.4)
        return None

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()


def lan_address(port: int) -> str | None:
    """Адрес машины в локальной сети — резерв, если туннель недоступен."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # пакет не уходит, нужен только маршрут
        host = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    return f"http://{host}:{port}" if host else None


def check_from_outside(
    base_url: str, gate_user: str, gate_password: str, attempts: int = 6
) -> tuple[bool, bool]:
    """Самопроверка публичного адреса: аноним должен получить 401, свой — 200.

    Свежеподнятый туннель отвечает не сразу (DNS trycloudflare расходится
    секунды), поэтому проверяем с повторами: одиночный запрос давал ложную
    тревогу «проверить гейт», хотя гейт был в порядке.
    """
    import base64  # noqa: PLC0415

    credentials = base64.b64encode(f"{gate_user}:{gate_password}".encode()).decode()

    def status(headers: dict[str, str]) -> int:
        request = urllib.request.Request(f"{base_url}/api/v1/events", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
        except OSError:
            return 0

    anonymous_ok = False
    authorized_ok = False
    for attempt in range(attempts):
        anonymous_ok = status({}) in (401, 403)
        authorized_ok = status({"Authorization": f"Basic {credentials}"}) == 200
        if anonymous_ok and authorized_ok:
            break
        if attempt < attempts - 1:
            time.sleep(2 + attempt * 2)
    return anonymous_ok, authorized_ok


# --- main -------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Поднять афишу наружу через Cloudflare Tunnel")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-tunnel", action="store_true", help="не поднимать туннель (только локально)")
    parser.add_argument("--lan", action="store_true", help="слушать 0.0.0.0 и показать адрес в сети")
    parser.add_argument("--fresh-db", action="store_true", help="пересоздать копию базы из рабочей")
    args = parser.parse_args()

    # Консоль Windows по умолчанию в cp1251 — без этого кириллица падает с ошибкой.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    print("\n=== Публичный показ афиши ===\n")
    print("1. Секреты и пароли приглашения")
    secrets_map = ensure_secrets()
    print(f"  файл: {ENV_FILE}")

    print("2. Копия базы для показа")
    public_db = ensure_public_db(args.fresh_db)
    database_url = f"sqlite:///{public_db.as_posix()}"
    accounts = ensure_demo_accounts(database_url, secrets_map)
    for role, email, _ in accounts:
        print(f"  {role}: {email}")

    print("3. Backend")
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    environment = dict(os.environ)
    environment.update(
        {
            "AFISHA_DATABASE_URL": database_url,
            "AFISHA_SECRET_KEY": secrets_map["AFISHA_SECRET_KEY"],
            "AFISHA_GATE_USER": secrets_map["AFISHA_GATE_USER"],
            "AFISHA_GATE_PASSWORD": secrets_map["AFISHA_GATE_PASSWORD"],
            "AFISHA_SERVE_STATIC": "1",  # фронт отдаёт тот же процесс: один origin
            "AFISHA_PUBLIC_MODE": "1",  # Swagger/OpenAPI закрыты, CORS выключен
        }
    )
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            host,
            "--port",
            str(args.port),
            "--log-level",
            "warning",
        ],
        cwd=str(BACKEND_DIR),
        env=environment,
    )
    if not wait_for_health(args.port, timeout=45):
        print("\nBackend не поднялся за 45 секунд. Логи — выше (uvicorn --log-level warning).")
        server.terminate()
        return 1
    print(f"  готов: http://127.0.0.1:{args.port}")

    tunnel: Tunnel | None = None
    public_url: str | None = None
    if not args.no_tunnel:
        print("4. Туннель наружу (cloudflared)")
        ensure_cloudflared()
        tunnel = Tunnel(args.port)
        public_url = tunnel.start()
        if not public_url:
            # QUIC (UDP 7844) режут многие провайдеры — пробуем HTTP/2.
            print("  QUIC не прошёл, пробую HTTP/2…")
            tunnel.stop()
            tunnel = Tunnel(args.port)
            public_url = tunnel.start(protocol="http2")
        if public_url:
            print(f"  {public_url}")
        else:
            print("  туннель не поднялся: показ только в локальной сети")
            with tunnel._lock:
                tail = tunnel._lines[-5:]
            for line in tail:
                print(f"    | {line}")
            tunnel.stop()
            tunnel = None

    lines: list[str] = []
    if public_url:
        print("5. Самопроверка публичного адреса")
        anonymous_ok, authorized_ok = check_from_outside(
            public_url, secrets_map["AFISHA_GATE_USER"], secrets_map["AFISHA_GATE_PASSWORD"]
        )
        print(f"  без пароля: {'401 (ок)' if anonymous_ok else 'НЕ 401 — проверить гейт!'}")
        print(f"  с паролем:  {'200 (ок)' if authorized_ok else 'НЕ 200 — проверить доступ!'}")
        lines += [
            "Ссылка (открыть в браузере, пароль спросит один раз):",
            f"  {public_url}",
            "",
            f"Вход в сам сайт (HTTP Basic): {secrets_map['AFISHA_GATE_USER']} / {secrets_map['AFISHA_GATE_PASSWORD']}",
        ]
    else:
        local = f"http://127.0.0.1:{args.port}"
        lines.append(f"Локально: {local}")
        if args.lan:
            lan = lan_address(args.port)
            if lan:
                lines.append(f"В локальной сети: {lan}")
    lines += ["", "Аккаунты на сайте:"]
    for role, email, password in accounts:
        lines.append(f"  {role}: {email} / {password}")
    lines += [
        "",
        "Данные показа — копия базы, рабочие события не затрагиваются.",
        "Остановить: Ctrl+C в этом окне.",
    ]

    print("\n" + "\n".join(lines))
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    INVITE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nПриглашение сохранено: {INVITE_FILE}")

    try:
        while server.poll() is None:
            time.sleep(1)
        print("\nBackend завершился сам — останавливаю туннель.")
    except KeyboardInterrupt:
        print("\nОстанавливаю…")
    finally:
        if tunnel:
            tunnel.stop()
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
