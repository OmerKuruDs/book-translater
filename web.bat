@echo off
rem Start the local web UI without having to activate the virtual environment first.
rem Double-click this file, or run it from any shell.

setlocal
set "HERE=%~dp0"
set "VENV=%HERE%.venv\Scripts\python.exe"

if not exist "%VENV%" (
    echo .venv bulunamadi: %VENV%
    echo.
    echo Once sanal ortami kurun:
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -e ".[web]"
    echo.
    pause
    exit /b 1
)

echo book-translator web arayuzu baslatiliyor...
echo Adres asagida yazacak; tarayicidan acin.
echo Durdurmak icin bu pencerede Ctrl+C.
echo.

"%VENV%" -m book_translator.web %*
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
    echo.
    echo Sunucu %CODE% koduyla kapandi.
    pause
)
exit /b %CODE%
