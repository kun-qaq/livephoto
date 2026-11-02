@echo off
rem Build a single-file Windows exe with PyInstaller.
rem Requires: pip install pillow imageio-ffmpeg sv-ttk pywinstyles pyinstaller
pyinstaller --noconfirm --onefile --noconsole --name LivePhotoMaker ^
  --icon icon.ico ^
  --collect-data imageio_ffmpeg ^
  --collect-data sv_ttk ^
  --exclude-module imageio ^
  --exclude-module numpy ^
  --exclude-module matplotlib ^
  livephoto.py
echo.
echo Done. See dist\LivePhotoMaker.exe
pause
