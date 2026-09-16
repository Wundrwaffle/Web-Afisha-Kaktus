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
         --lan (слушать 0.0.0.0 и показать адрес в сети),
         --stop (погасить хвосты прошлого показа и освободить порт)

Самолечение при повторном запуске (иначе «одна кнопка» ломается):
  * порт занят нашим же процессом показа (окно закрыли крестиком) — гасим его,
  * порт занят чужим процессом — не трогаем, говорим и просим другой порт,
  * проверяем, что на порту отвечает именно наш сервер В ПУБЛИЧНОМ режиме
    (health 200, /openapi.json 404, сайт 401) — иначе показ наружу не поднимаем,
  * упавший туннель поднимаем заново и перевыпускаем приглашение с новым адресом.
"""

from __future__ import annotations

import argparse
import json
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
PID_FILE = SECRETS_DIR / "expose.pids"
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


def human_age(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    return f"{hours} ч" if hours < 24 else f"{hours // 24} дн"


def ensure_public_db(fresh: bool) -> Path:
    if PUBLIC_DB.exists() and not fresh:
        age = human_age(time.time() - PUBLIC_DB.stat().st_mtime)
        print(f"  используется прежняя копия (сделана {age} назад; expose.bat fresh — обновить)")
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


# --- порт, свои процессы, самопроверка --------------------------------------


def _run(command: list[str]) -> str:
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=25, encoding="utf-8", errors="replace"
        )
        return done.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def port_is_busy(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((host, port)) == 0


def listener_pid(port: int) -> int | None:
    """PID процесса, слушающего порт (netstat -ano: последний столбец — PID)."""
    for line in _run(["netstat", "-ano", "-p", "TCP"]).splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[1].endswith(f":{port}") and fields[-1].isdigit():
            return int(fields[-1])
    return None


def process_alive(pid: int) -> bool:
    """Есть ли процесс с таким PID.

    При отсутствии процесса tasklist печатает «INFO: No tasks are running…» —
    не пустую строку. Наивная проверка «вывод непустой» всегда возвращала True,
    и код пытался убить уже мёртвый PID, а потом сообщал о неудаче.
    """
    if not pid:
        return False
    output = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]).strip()
    if not output.startswith('"'):
        return False
    fields = output.splitlines()[0].split(",")
    return len(fields) > 1 and fields[1].strip('"').strip() == str(pid)


def process_name(pid: int) -> str:
    output = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]).strip()
    if not output.startswith('"'):
        return ""
    return output.splitlines()[0].split(",")[0].strip('" \r\n').lower()


def kill_process(pid: int) -> bool:
    _run(["taskkill", "/PID", str(pid), "/F", "/T"])
    for _ in range(12):
        if not process_alive(pid):
            return True
        time.sleep(0.3)
    return False


def stop_process_tree(process: subprocess.Popen) -> None:
    """Гасит процесс вместе с потомками.

    Ловушка Windows: `python.exe` из venv, созданного uv, — лаунчер-трамплин,
    он порождает реальный интерпретатор дочерним процессом. terminate() по PID
    лаунчера оставляет сервер живым держать порт (ловушка «зомби» наоборот:
    показ вроде остановлен, а health отвечает старый процесс).
    """
    if process.poll() is not None:
        return
    kill_process(process.pid)  # taskkill /F /T — вместе с потомками
    if process.poll() is None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


# Процессы, которые заводит сам показ. Чужие на порту не трогаем.
OUR_PROCESSES = ("python.exe", "python3.exe", "py.exe", "uvicorn.exe", "cloudflared.exe")


def save_pids(**pids: int | None) -> None:
    data = {key: pid for key, pid in pids.items() if pid}
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(json.dumps(data), encoding="utf-8")


def cleanup_own_processes() -> None:
    """Гасит хвосты прошлого показа.

    Если окно закрыли крестиком, дочерние процессы (uvicorn, cloudflared)
    остаются жить: они держат порт, новый сервер не может встать, а health
    бодро отвечает 200 от СТАРОГО процесса — и показ уходит другу со старым
    кодом. Ловим их по файлу с PID.
    """
    if not PID_FILE.exists():
        return
    try:
        data = json.loads(PID_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    for name, pid in (data or {}).items():
        if not isinstance(pid, int) or not process_alive(pid):
            continue
        # PID мог быть переиспользован системой — гасим только процессы нашего вида.
        if process_name(pid) not in OUR_PROCESSES:
            continue
        if kill_process(pid):
            print(f"  остановлен прежний процесс показа: {name} (pid {pid})")
        else:
            print(f"  не удалось остановить {name} (pid {pid}) — закройте его вручную")
    PID_FILE.unlink(missing_ok=True)


def free_port(port: int) -> bool:
    """Проверяет, что порт свободен; свой процесс показа — убирает, чужой — не трогает."""
    if not port_is_busy(port):
        return True
    pid = listener_pid(port)
    name = process_name(pid) if pid else ""
    if pid and name in OUR_PROCESSES:
        print(f"  порт {port} занят нашим процессом {name} (pid {pid}) — останавливаю")
        kill_process(pid)
        for _ in range(20):
            if not port_is_busy(port):
                return True
            time.sleep(0.5)
    who = name or "неизвестный процесс"
    print(f"  порт {port} занят: {who}" + (f", pid {pid}" if pid else ""))
    print(f"  закройте его или запустите показ на другом порту: expose.bat <порт>")
    return False


def check_local_public_mode(port: int, gate_user: str, gate_password: str) -> bool:
    """Проверяет, что на порту наш сервер В ПУБЛИЧНОМ режиме.

    Признаки публичного режима: health — 200 (кроме гейта), корень сайта
    анонимно — 401, /openapi.json С ПАРОЛЕМ ГЕЙТА — 404 (Swagger выключен в
    приложении). Без пароля /openapi.json отдаёт 401: гейт отвечает раньше
    маршрутизации, поэтому анонимное ожидание 404 давало ложную тревогу
    «Swagger открыт» и блокировало показ на живом сервере.
    """
    import base64  # noqa: PLC0415

    credentials = base64.b64encode(f"{gate_user}:{gate_password}".encode()).decode()

    def status(path: str, headers: dict[str, str] | None = None) -> int:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", headers=headers or {}
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
        except OSError:
            return 0

    health = status("/api/v1/health")
    root = status("/")
    openapi_anonymous = status("/openapi.json")
    openapi_authorized = status("/openapi.json", {"Authorization": f"Basic {credentials}"})
    print(f"  health: {health} {'(ок)' if health == 200 else '(ПЛОХО)'}")
    print(
        f"  сайт анонимно: {root} "
        f"{'(ок — под гейтом)' if root == 401 else '(ПЛОХО — открыт!)'}"
    )
    print(
        f"  /openapi.json анонимно: {openapi_anonymous} "
        f"{'(ок — гейт закрыл)' if openapi_anonymous == 401 else '(ПЛОХО — доступен!)'}"
    )
    print(
        f"  /openapi.json с паролем: {openapi_authorized} "
        f"{'(ок — выключен)' if openapi_authorized == 404 else '(ПЛОХО — открыт!)'}"
    )
    return (
        health == 200
        and root == 401
        and openapi_anonymous == 401
        and openapi_authorized == 404
    )


def copy_to_clipboard(text_file: Path) -> bool:
    """Кладёт приглашение в буфер обмена — его удобно сразу отправить в мессенджер.

    Через файл и PowerShell: `clip` перекодирует кириллицу в OEM-кодировку и
    ломает текст, а Set-Clipboard с -Encoding UTF8 читает файл как есть.
    """
    try:
        done = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"Set-Clipboard -Value (Get-Content -LiteralPath '{text_file}' -Raw -Encoding UTF8)",
            ],
            capture_output=True,
            text=True,
            timeout=25,
        )
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def publish_invite(lines: list[str]) -> None:
    """Печатает приглашение, пишет файл и кладёт его в буфер обмена."""
    text = "\n".join(lines) + "\n"
    print("\n" + text)
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    INVITE_FILE.write_text(text, encoding="utf-8")
    print(f"Приглашение сохранено: {INVITE_FILE}")
    if copy_to_clipboard(INVITE_FILE):
        print("Скопировано в буфер обмена — можно сразу вставлять в мессенджер.")


# --- main -------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Поднять афишу наружу через Cloudflare Tunnel")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-tunnel", action="store_true", help="не поднимать туннель (только локально)")
    parser.add_argument("--lan", action="store_true", help="слушать 0.0.0.0 и показать адрес в сети")
    parser.add_argument("--fresh-db", action="store_true", help="пересоздать копию базы из рабочей")
    parser.add_argument("--stop", action="store_true", help="остановить показ и освободить порт")
    args = parser.parse_args()

    # Консоль Windows по умолчанию в cp1251 — без этого кириллица падает с ошибкой.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    if args.stop:
        print("\n=== Остановка публичного показа ===")
        cleanup_own_processes()
        if port_is_busy(args.port):
            free_port(args.port)
        print("Порт свободен." if not port_is_busy(args.port) else "Порт всё ещё занят — см. выше.")
        return 0

    print("\n=== Публичный показ афиши ===\n")
    print(f"0. Порт {args.port}")
    cleanup_own_processes()  # хвосты прошлого запуска держат порт и портят показ
    if not free_port(args.port):
        return 1
    print("  свободен")

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
        stop_process_tree(server)
        return 1
    print(f"  готов: http://127.0.0.1:{args.port}")
    if not check_local_public_mode(
        args.port, secrets_map["AFISHA_GATE_USER"], secrets_map["AFISHA_GATE_PASSWORD"]
    ):
        print("\nНа этом порту отвечает НЕ наш публичный сервер: гейт не работает")
        print("и Swagger открыт — показ наружу в таком виде не поднимаю.")
        stop_process_tree(server)
        return 1
    save_pids(server=server.pid)

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

    def build_lines(url: str | None) -> list[str]:
        """Текст приглашения.

        Отдельной функцией, потому что при перезапуске туннеля адрес меняется
        и приглашение приходится выпускать заново.
        """
        lines: list[str] = []
        if url:
            lines += [
                "Ссылка (открыть в браузере, пароль спросит один раз):",
                f"  {url}",
                "",
                f"Вход в сам сайт (HTTP Basic): {secrets_map['AFISHA_GATE_USER']} / {secrets_map['AFISHA_GATE_PASSWORD']}",
            ]
        else:
            lines.append(f"Локально: http://127.0.0.1:{args.port}")
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
        return lines

    if public_url:
        print("5. Самопроверка публичного адреса")
        anonymous_ok, authorized_ok = check_from_outside(
            public_url, secrets_map["AFISHA_GATE_USER"], secrets_map["AFISHA_GATE_PASSWORD"]
        )
        print(f"  без пароля: {'401 (ок)' if anonymous_ok else 'НЕ 401 — проверить гейт!'}")
        print(f"  с паролем:  {'200 (ок)' if authorized_ok else 'НЕ 200 — проверить доступ!'}")

    publish_invite(build_lines(public_url))
    save_pids(server=server.pid, tunnel=tunnel.process.pid if tunnel and tunnel.process else None)

    restarts = 0
    try:
        while server.poll() is None:
            # Сторож туннеля: quick-туннели живут нестабильно, а молча упавший
            # туннель выглядит как «ссылка не открывается» на стороне гостя.
            if tunnel is not None and tunnel.process is not None and tunnel.process.poll() is not None:
                if restarts >= 3:
                    print("\nТуннель не восстановился — показ остаётся только локальным.")
                    tunnel = None
                else:
                    restarts += 1
                    print(f"\nТуннель отвалился — поднимаю заново (попытка {restarts}/3)…")
                    tunnel.stop()
                    tunnel = Tunnel(args.port)
                    public_url = tunnel.start() or None
                    if not public_url:
                        tunnel.stop()
                        tunnel = Tunnel(args.port)
                        public_url = tunnel.start(protocol="http2")
                    if public_url:
                        print(f"  новая ссылка: {public_url}")
                        print("  Внимание: адрес изменился — отправьте другу новую ссылку.")
                        publish_invite(build_lines(public_url))
                        save_pids(
                            server=server.pid,
                            tunnel=tunnel.process.pid if tunnel.process else None,
                        )
                    else:
                        print("  не получилось — попробую снова через 10 секунд")
                        time.sleep(10)
            time.sleep(1)
        print("\nBackend завершился сам — останавливаю туннель.")
    except KeyboardInterrupt:
        print("\nОстанавливаю…")
    finally:
        if tunnel:
            tunnel.stop()
        stop_process_tree(server)
        PID_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
