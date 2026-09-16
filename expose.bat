@echo off
REM Публичный показ афиши для товарища: копия базы, гейт по паролю, туннель наружу.
REM Запуск двойным щелчком. Ссылка, пароль гейта и демо-аккаунты печатаются в окно.
REM Остановить показ — Ctrl+C в этом окне.
chcp 65001 > NUL
cd /d "%~dp0"
title Публичный показ афиши

where python > NUL 2>&1
if errorlevel 1 (
  echo.
  echo Python не найден в PATH. Поставьте Python 3.11+ и запустите снова.
  echo.
  pause
  exit /b 1
)

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8098"

echo.
echo ==========================================
echo   Публичный показ афиши, порт %PORT%
echo ==========================================
echo.
echo Сейчас: копия базы -^> сервер -^> туннель cloudflared.
echo Первый запуск качает cloudflared (~35 МБ), это не быстро.
echo.
echo Пароли и демо-аккаунты сохраняются в backend\..\.secrets\expose.env
echo и переиспользуются - приглашение остаётся тем же.
echo.

python backend\expose.py --port %PORT%
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" (
  echo Показ завершился с кодом %CODE%. Если в выводе выше есть ошибка - читайте её.
) else (
  echo Показ остановлен.
)
echo.
pause
