@echo off
setlocal
chcp 866 >nul
title Web-Afisha-Kaktus dev

cd /d "%~dp0"

set "BACK_PORT=8000"
set "FRONT_PORT=8080"

echo ============================================
echo   Web-Afisha-Kaktus - запуск dev-окружения
echo ============================================
echo.

where python > NUL 2>&1
if errorlevel 1 (
  echo Python не найден в PATH. Поставьте Python 3.11+ и запустите снова.
  echo.
  pause
  exit /b 1
)

rem --- проверка ВСЕХ зависимостей из backend\requirements.txt (раньше проверялись
rem --- только uvicorn+fastapi: у пользователя sqlalchemy не стояла, проверка
rem --- проходила, и показ падал позже на ModuleNotFoundError):
python backend\check_deps.py
if errorlevel 1 (
  echo Зависимости бэкенда не установлены - ставлю из backend\requirements.txt...
  echo.
  python -m pip install -r backend\requirements.txt
  if errorlevel 1 (
    echo.
    echo Установка не прошла - смотрите вывод выше.
    echo.
    pause
    exit /b 1
  )
)

rem --- порты: занятый порт - главная причина "сервер отвечает старым кодом" ---
python -c "import socket,sys; sys.exit(0 if socket.socket().connect_ex(('127.0.0.1',%BACK_PORT%))==0 else 1)"
if not errorlevel 1 (
  echo ВНИМАНИЕ: порт %BACK_PORT% уже занят. Скорее всего, остался прошлый запуск:
  echo тогда новый сервер НЕ встанет, а браузер будет работать со старым кодом.
  netstat -ano -p TCP | findstr ":%BACK_PORT%"
  echo   Погасить чужой процесс:  taskkill /F /PID ^<PID из таблицы выше^>
  echo.
)
python -c "import socket,sys; sys.exit(0 if socket.socket().connect_ex(('127.0.0.1',%FRONT_PORT%))==0 else 1)"
if not errorlevel 1 (
  echo ВНИМАНИЕ: порт %FRONT_PORT% уже занят - проверьте, не запущен ли уже фронт.
  netstat -ano -p TCP | findstr ":%FRONT_PORT%"
  echo.
)

echo [1/3] Бэкенд на порту %BACK_PORT% (с автоперезагрузкой)...
start "backend (FastAPI :%BACK_PORT%)" cmd /k "cd /d %~dp0backend && python -m uvicorn app.main:app --reload --port %BACK_PORT%"

echo [2/3] Фронтенд на порту %FRONT_PORT% (статика из docs)...
start "frontend (:%FRONT_PORT%)" cmd /k "cd /d %~dp0docs && python -m http.server %FRONT_PORT%"

echo [3/3] Жду, пока бэкенд ответит на /api/v1/health...
set /a TRIES=0
:wait
set /a TRIES+=1
timeout /t 1 /nobreak >nul
curl -s -o NUL "http://127.0.0.1:%BACK_PORT%/api/v1/health"
if not errorlevel 1 goto ready
if %TRIES% LSS 30 goto wait
echo.
echo Бэкенд не ответил за 30 секунд - смотрите его окно (там ошибка).
echo.
pause
exit /b 1

:ready
start "" "http://localhost:%FRONT_PORT%/index.html"
echo.
echo Готово. Открыты два окна (backend + frontend) и браузер.
echo   API / Swagger:  http://localhost:%BACK_PORT%/docs
echo   Сайт:           http://localhost:%FRONT_PORT%/index.html
echo.
echo Правки index.html видны после F5, бэкенд перезагружается сам.
echo Публичный показ наружу - expose.bat.
echo.
echo Остановить: закройте окна backend и frontend.
endlocal
