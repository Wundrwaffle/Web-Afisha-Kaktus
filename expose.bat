@echo off
REM Публичный показ афиши для товарища: копия базы, гейт по паролю, туннель наружу.
REM Запуск двойным щелчком. Ссылка, пароль гейта и демо-аккаунты печатаются в окно.
REM
REM   expose.bat           обычный запуск (порт 8098)
REM   expose.bat 8099      другой порт
REM   expose.bat fresh     обновить копию базы из рабочей (и обнулить демо-данные)
REM   expose.bat lan       добавить адрес в локальной сети (если туннель недоступен)
REM   expose.bat stop      остановить показ и освободить порт
REM
REM Остановить показ - Ctrl+C в этом окне (или expose.bat stop, если окно потерялось).
chcp 866 > NUL
cd /d "%~dp0"
title Публичный показ афиши
setlocal

set "PORT=8098"
set "EXTRA="

:parse
if "%~1"=="" goto checks
set "ARG=%~1"
echo %ARG%|findstr /r "^[0-9][0-9]*$" >NUL
if not errorlevel 1 set "PORT=%ARG%"
if /i "%ARG%"=="fresh" set "EXTRA=%EXTRA% --fresh-db"
if /i "%ARG%"=="lan"   set "EXTRA=%EXTRA% --lan"
if /i "%ARG%"=="stop"  set "EXTRA=%EXTRA% --stop"
shift
goto parse

:checks
where python > NUL 2>&1
if errorlevel 1 (
  echo.
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
  echo.
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

echo.
echo ==========================================
echo   Публичный показ афиши, порт %PORT%
echo ==========================================
echo.
echo Что будет: копия базы -^> сервер -^> туннель cloudflared.
echo Первый запуск качает cloudflared (~35 МБ), это не быстро.
echo Проверки при старте: порт, публичный режим сервера, доступ снаружи.
echo.
echo Ссылка, пароль входа и демо-аккаунты в конце вывода; они же - в буфере обмена,
echo .secrets\invite.txt и в окне ниже. Приглашение переживает перезапуск:
echo пароли берутся из .secrets\expose.env, старые ссылки не нужны.
echo.

python backend\expose.py --port %PORT% %EXTRA%
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" (
  echo Показ завершился с кодом %CODE%. Ошибка - в выводе выше.
) else (
  echo Показ остановлен.
)
echo.
pause
