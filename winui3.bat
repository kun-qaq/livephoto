@echo off
rem WinUI3 launcher / builder for LivePhotoMaker.
rem Usage: winui3.bat run   (start from source)
rem        winui3.bat build (rebuild the exe)
cd /d "%~dp0"
if /i "%~1"=="build" goto build
if /i "%~1"=="run" goto run
echo Usage: winui3.bat run ^| build
goto end

:run
if not exist ".venv\Scripts\pythonw.exe" (
  echo [ERROR] .venv\Scripts\pythonw.exe not found
  goto end
)
start "" ".venv\Scripts\pythonw.exe" "livephoto.py"
goto end

:build
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv\Scripts\python.exe not found
  goto end
)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm build_winui3.spec
echo Done. See dist\

:end
