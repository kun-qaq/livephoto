@echo off
rem Build a single-file Windows exe with PyInstaller.
rem Requires: pip install pillow imageio-ffmpeg customtkinter pyinstaller
pyinstaller --noconfirm --onefile --noconsole --name LivePhotoMaker ^
  --icon icon.ico ^
  --collect-data imageio_ffmpeg ^
  --collect-data customtkinter ^
  --exclude-module imageio ^
  --exclude-module numpy ^
  --exclude-module matplotlib ^
  livephoto.py
echo.
echo Done. See dist\LivePhotoMaker.exe
pause
