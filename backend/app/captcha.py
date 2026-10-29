"""Яндекс SmartCaptcha: конфигурация виджета и серверная проверка токена.

Включается только ключами окружения (`AFISHA_SMARTCAPTCHA_CLIENT_KEY` и
`AFISHA_SMARTCAPTCHA_SERVER_KEY`): без пары ключей проверка выключена, и dev,
тесты и локальный показ работают как раньше. Ключ сервера никогда не уходит на
фронт — наружу отдаётся только клиентский.

Токен виджета одноразовый и живёт 5 минут, поэтому проверка идёт на каждый
запрос регистрации отдельно, а после ошибки фронт сбрасывает виджет.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

DEFAULT_VALIDATE_URL = "https://smartcaptcha.cloud.yandex.ru/validate"

# Один текст на все формы: фронт показывает его как есть, поэтому формулировка
# общая и без привязки к конкретному действию.
CAPTCHA_FAILED_MESSAGE = (
    "Не удалось подтвердить, что вы не робот. Пройдите проверку заново."
)


class CaptchaConfig:
    """Пара ключей одной капчи из консоли Яндекс Облака."""

    def __init__(self, client_key: str = "", server_key: str = ""):
        self.client_key = (client_key or "").strip()
        self.server_key = (server_key or "").strip()

    @property
    def enabled(self) -> bool:
        # Ключи выдаются парой: один без другого — это недонастроенная капча,
        # и молча «включать половину» нельзя.
        return bool(self.client_key and self.server_key)


def config_from_env() -> CaptchaConfig:
    return CaptchaConfig(
        client_key=os.environ.get("AFISHA_SMARTCAPTCHA_CLIENT_KEY", ""),
        server_key=os.environ.get("AFISHA_SMARTCAPTCHA_SERVER_KEY", ""),
    )


def build_verifier(config: CaptchaConfig) -> "CaptchaVerifier":
    """Проверяльщик из конфигурации.

    Адрес validate можно переопределить окружением — это нужно, чтобы прогнать
    сквозной сценарий регистрации локально, без реального Яндекса и его домена.
    """
    return CaptchaVerifier(
        config.server_key,
        validate_url=os.environ.get(
            "AFISHA_SMARTCAPTCHA_VALIDATE_URL", DEFAULT_VALIDATE_URL
        ),
    )


class CaptchaVerifier:
    """POST на validate: `secret` + `token` (+ `ip` для качества моделей).

    Ответ — JSON со `status`: `ok` — человек, `failed` — робот или испорченный
    токен. Ошибки HTTP/сети трактуются как «проверка пройдена»: так рекомендует
    документация Яндекса, иначе недоступность чужого сервиса останавливает
    регистрацию на нашем сайте (потерю живых пользователей цена выше спама).
    """

    def __init__(
        self,
        server_key: str,
        *,
        validate_url: str = DEFAULT_VALIDATE_URL,
        timeout: float = 5.0,
    ):
        self.server_key = server_key
        self.validate_url = validate_url
        self.timeout = timeout

    def _post(self, payload: dict[str, str]) -> dict:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            self.validate_url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def verify(self, token: str | None, ip: str | None = None) -> bool:
        token = (token or "").strip()
        if not token:
            # Пустой токен — виджет не пройден; в сеть за этим не ходим.
            return False
        payload = {"secret": self.server_key, "token": token}
        if ip:
            payload["ip"] = ip
        try:
            result = self._post(payload)
        except Exception as exc:  # сеть, таймаут, не-200, битый JSON
            logger.warning("SmartCaptcha недоступна (%s) — пропускаем проверку", exc)
            return True
        if str(result.get("status", "")).strip().lower() == "ok":
            return True
        message = result.get("message") or ""
        if message:
            # message — только для диагностики: сравнивать его в коде нельзя.
            logger.warning("SmartCaptcha отклонила токен: %s", message)
        return False


def check_captcha(
    verifier: "CaptchaVerifier | None",
    session,
    *,
    smart_token: str | None = None,
    local_token: str | None = None,
    local_nonce: str | None = None,
    ip: str | None = None,
) -> bool:
    """Прохождение проверки любым из двух способов.

    Сначала обычный путь — токен виджета Яндекса. Если токена нет (скрипт
    срезал блокировщик), принимаем решение своей капчи из `local_captcha`.
    Оба варианта одноразовые, поэтому порядок проверки ничего не ослабляет:
    важно только, что человек заплатил за прохождение — вниманием или
    вычислениями.
    """
    from . import local_captcha

    value = (smart_token or "").strip()
    if value and verifier is not None and verifier.verify(value, ip):
        return True
    if (local_token or "").strip() and (local_nonce or "").strip():
        return local_captcha.verify(session, local_token, local_nonce, ip)
    return False

