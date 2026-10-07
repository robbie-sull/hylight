@echo off
rem Builds the Windows HyLight app with uv (no other setup needed).
rem Output: dist\HyLight\HyLight.exe and dist\HyLight-Windows-0.6.0.zip
rem (the folder plus the tester guide, ready to hand out).
cd /d "%~dp0"

uv run --no-project --python 3.13 ^
  --with pyinstaller --with pydexcom==0.5.1 --with hidapi --with keyring ^
  --with flask --with pystray --with pillow ^
  pyinstaller --noconfirm --clean HyLight_win.spec
if errorlevel 1 goto :fail

copy /y "pilot_release\Read Me First (Windows).txt" "dist\Read Me First.txt" >nul
powershell -NoProfile -Command "Compress-Archive -Force -Path 'dist\HyLight','dist\Read Me First.txt' -DestinationPath 'dist\HyLight-Windows-0.6.0.zip'"
if errorlevel 1 goto :fail

echo.
echo Built dist\HyLight\HyLight.exe and dist\HyLight-Windows-0.6.0.zip
exit /b 0

:fail
echo.
echo BUILD FAILED - copy everything above and send it back.
exit /b 1
