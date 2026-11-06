@echo off
rem ============================================================
rem  Вечный перезапуск бота Steam-скидок (24/7).
rem  Запускает бота без окна консоли (pythonw.exe) и, если процесс
rem  завершился (крэш, обновление), поднимает его через 10 секунд.
rem  Окно сторожа можно свернуть — бот продолжает жить.
rem ============================================================
setlocal
cd /d "%~dp0"

set PY=%~dp0.venv\Scripts\pythonw.exe
if not exist "%PY%" set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" set PY=pythonw.exe

:loop
echo [%date% %time%] запускаю бота (%PY%) >> logs\forever.log
start "" /b "%PY%" run_bot.py

rem ждём и проверяем, что процесс действительно жив
timeout /t 20 /nobreak >nul
tasklist /FI "IMAGENAME eq pythonw.exe" /NH | find /I "pythonw.exe" >nul
if errorlevel 1 (
  tasklist /FI "IMAGENAME eq python.exe" /NH | find /I "python.exe" >nul
  if errorlevel 1 (
    echo [%date% %time%] процесс не найден, перезапускаю >> logs\forever.log
    timeout /t 10 /nobreak >nul
    goto loop
  )
)

echo [%date% %time%] бот работает. Сторож выходит. >> logs\forever.log
endlocal
