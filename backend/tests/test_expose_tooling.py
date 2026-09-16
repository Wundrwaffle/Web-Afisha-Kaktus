"""Тесты обвязки публичного показа (backend/expose.py).

Проверяем то, без чего «одна кнопка» ломается и показ уходит наружу не тем
сервером:

- занятость порта определяется верно (иначе новый сервер не встаёт, а health
  отвечает старый процесс — ловушка «зомби»);
- хвосты прошлого показа (uvicorn/cloudflared, оставшиеся после закрытия окна
  крестиком) распознаются и убираются, а испорченный файл с PID не ломает запуск;
- живость PID определяется по CSV-строке tasklist: при отсутствии процесса
  tasklist печатает «INFO: No tasks are running…», и наивная проверка «вывод
  непустой» считала мёртвый PID живым;
- режим сервера на порту распознаётся верно: /openapi.json анонимно отдаёт 401
  от гейта, поэтому ожидание 404 без пароля объявляло живой публичный сервер
  чужим и глушило показ (проверка щупает Swagger с паролем гейта);
- в приглашении перечислены все роли, включая админа: аккаунт, обещанный гостю,
  обязан реально создаваться в копии базы, а пароль из файла секретов — работать.
"""
import base64
import sys
import http.server
import socket
import threading

import pytest

import expose


def test_save_pids_skips_empty_values(tmp_path, monkeypatch):
    monkeypatch.setattr(expose, "SECRETS_DIR", tmp_path)
    monkeypatch.setattr(expose, "PID_FILE", tmp_path / "expose.pids")

    expose.save_pids(server=1234, tunnel=None)

    assert expose.PID_FILE.read_text(encoding="utf-8") == '{"server": 1234}'


def test_cleanup_removes_stale_file_with_dead_pids(tmp_path, monkeypatch):
    pid_file = tmp_path / "expose.pids"
    pid_file.write_text('{"server": 999999, "tunnel": 999998}', encoding="utf-8")
    monkeypatch.setattr(expose, "SECRETS_DIR", tmp_path)
    monkeypatch.setattr(expose, "PID_FILE", pid_file)

    expose.cleanup_own_processes()

    assert not pid_file.exists()


def test_cleanup_tolerates_broken_pid_file(tmp_path, monkeypatch):
    pid_file = tmp_path / "expose.pids"
    pid_file.write_text("не json", encoding="utf-8")
    monkeypatch.setattr(expose, "SECRETS_DIR", tmp_path)
    monkeypatch.setattr(expose, "PID_FILE", pid_file)

    expose.cleanup_own_processes()  # не должно падать

    assert not pid_file.exists()


def test_cleanup_without_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(expose, "SECRETS_DIR", tmp_path)
    monkeypatch.setattr(expose, "PID_FILE", tmp_path / "нет-такого.pids")

    expose.cleanup_own_processes()


def test_process_alive_false_for_unknown_pid():
    # Именно на этом спотыкался kill_process: tasklist печатает INFO-строку.
    assert expose.process_alive(999999) is False
    assert expose.process_name(999999) == ""
    assert expose.process_alive(0) is False


def test_port_is_busy_sees_listener_and_release():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        assert expose.port_is_busy(port) is True
        assert expose.listener_pid(port) is not None

    assert expose.port_is_busy(port) is False


def test_free_port_true_for_free_port():
    with socket.socket() as probe:  # берём свободный порт у системы и сразу отдаём
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    assert expose.free_port(port) is True


def _stub_backend(public_mode: bool):
    """Мини-сервер, повторяющий поведение бэкенда для проверки режима.

    public_mode=True — как наш публичный режим: health открыт, остальное под
    Basic-гейтом, Swagger с паролем отдаёт 404. public_mode=False — как
    dev-сервер: всё открыто.
    """
    expected = "Basic " + base64.b64encode(b"friend:secret").decode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if not public_mode:
                return self._send(200)
            if self.path == "/api/v1/health":
                return self._send(200)
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="afisha"')
                self.end_headers()
                return
            return self._send(404 if self.path in ("/openapi.json", "/docs") else 200)

        def _send(self, code: int) -> None:
            self.send_response(code)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):  # не засоряем вывод тестов
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_public_mode_check_accepts_gated_server():
    # Гейт отвечает раньше маршрутизации: анонимно /openapi.json даёт 401, а
    # 404 (Swagger выключен) виден только с паролем гейта. Проверка, которая
    # щупала /openapi.json анонимно, объявляла живой сервер чужим и глушила показ.
    server = _stub_backend(public_mode=True)
    try:
        assert expose.check_local_public_mode(server.server_address[1], "friend", "secret") is True
    finally:
        server.shutdown()
        server.server_close()


def test_public_mode_check_rejects_dev_server():
    server = _stub_backend(public_mode=False)
    try:
        assert expose.check_local_public_mode(server.server_address[1], "friend", "secret") is False
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(30, "0 мин"), (90, "1 мин"), (3600, "1 ч"), (7200, "2 ч"), (3 * 86400, "3 дн")],
)
def test_human_age(seconds, expected):
    assert expose.human_age(seconds) == expected


def _demo_secrets() -> dict[str, str]:
    return {
        "AFISHA_GATE_USER": "afisha",
        "AFISHA_GATE_PASSWORD": "gate-pass",
        "DEMO_ADMIN_EMAIL": "admin-friend@example.com",
        "DEMO_ADMIN_NAME": "Админ (тест)",
        "DEMO_ADMIN_PASSWORD": "admin-pass",
        "DEMO_ORGANIZER_EMAIL": "friend@example.com",
        "DEMO_ORGANIZER_NAME": "Друг (тест)",
        "DEMO_ORGANIZER_PASSWORD": "org-pass",
        "DEMO_MODERATOR_EMAIL": "moderator-friend@example.com",
        "DEMO_MODERATOR_NAME": "Модератор (тест)",
        "DEMO_MODERATOR_PASSWORD": "mod-pass",
    }


def test_demo_accounts_include_admin_and_mute_dev_accounts(tmp_path):
    """Приглашение обещает админа — значит, аккаунт обязан создаваться.

    Заодно инвариант показа: активными в копии остаются только демо-аккаунты,
    а dev-админ рабочей базы гасится.
    """
    sys.path.insert(0, str(expose.BACKEND_DIR))
    from sqlalchemy import select  # noqa: PLC0415

    from app.db import Base, build_engine, build_session_factory  # noqa: PLC0415
    from app.models import User  # noqa: PLC0415
    from app.security import hash_password, verify_password  # noqa: PLC0415

    url = f"sqlite:///{(tmp_path / 'public.sqlite3').as_posix()}"
    secrets_map = _demo_secrets()

    engine = build_engine(url)
    Base.metadata.create_all(engine)
    with build_session_factory(engine)() as session:
        session.add(
            User(
                email="denis@gmail.ru",
                password_hash=hash_password("рабочий-пароль"),
                full_name="Денис",
                role="admin",
                is_active=True,
            )
        )
        session.commit()
    engine.dispose()

    created = expose.ensure_demo_accounts(url, secrets_map)

    assert created == [
        ("admin", "admin-friend@example.com", "admin-pass"),
        ("organizer", "friend@example.com", "org-pass"),
        ("moderator", "moderator-friend@example.com", "mod-pass"),
    ]

    engine = build_engine(url)
    with build_session_factory(engine)() as session:
        users = {u.email: u for u in session.scalars(select(User)).all()}
    engine.dispose()

    admin = users["admin-friend@example.com"]
    assert (admin.role, admin.is_active) == ("admin", True)
    assert admin.password_hash != "admin-pass"  # открытым текстом пароль не лежит
    assert verify_password("admin-pass", admin.password_hash) is True
    assert users["denis@gmail.ru"].is_active is False


def test_invite_names_credentials_of_every_role():
    """Ровно то, что гость видит в invite.txt: гейт и аккаунты по ролям."""
    accounts = [
        ("admin", "admin-friend@example.com", "admin-pass"),
        ("organizer", "friend@example.com", "org-pass"),
    ]

    text = "\n".join(
        expose.build_invite_lines(
            "https://example.trycloudflare.com", _demo_secrets(), accounts, port=8098
        )
    )

    assert "https://example.trycloudflare.com" in text
    assert "afisha / gate-pass" in text
    assert "admin: admin-friend@example.com / admin-pass" in text
    assert "organizer: friend@example.com / org-pass" in text


def test_invite_without_tunnel_stays_local():
    text = "\n".join(expose.build_invite_lines(None, _demo_secrets(), [], port=8098))

    assert "Локально: http://127.0.0.1:8098" in text
