@echo off
setlocal
chcp 65001 >nul
title Web-Afisha-Kaktus dev

cd /d "%~dp0"

echo ============================================
echo   Web-Afisha-Kaktus - запуск dev-окружения
echo ============================================
echo.

rem --- Бэкенд (FastAPI) ---
echo [1/3] Запускаю бэкенд на порту 8000...
start "backend (FastAPI :8000)" cmd /k "cd /d %~dp0backend && python -m uvicorn app.main:app --reload --port 8000"

rem --- Фронтенд (статический сервер из docs) ---
echo [2/3] Запускаю фронтенд на порту 8080...
start "frontend (:8080)" cmd /k "cd /d %~dp0docs && python -m http.server 8080"

rem --- Ждём поднятия серверов ---
echo [3/3] Жду 2 секунды и открываю браузер...
timeout /t 2 /nobreak >nul

start "" "http://localhost:8080/index.html"

echo.
echo Готово. Открыты два окна (backend + frontend) и браузер.
echo   API / Swagger:  http://localhost:8000/docs
echo   Сайт:           http://localhost:8080/index.html
echo.
echo Закройте консольные окна, чтобы остановить серверы.
endlocal