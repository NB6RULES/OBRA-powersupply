@echo off
rem Builds dist\PSU Studio.exe (single file, driver installer bundled)
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3) || (set PY=python)
%PY% -m pip install -q -r requirements.txt pyinstaller
%PY% tools\version_file.py 0 > version_info.txt
%PY% -m PyInstaller --noconfirm --onefile --windowed --name "PSU Studio" --icon "app\assets\psu_studio.ico" --version-file version_info.txt ^
  --add-data "app\web;web" --add-data "drivers;drivers" ^
  --paths app --hidden-import charger --hidden-import device --hidden-import driver --hidden-import importers --hidden-import logs --hidden-import updater ^
  app\psu_studio.py
if errorlevel 1 (echo BUILD FAILED & pause & exit /b 1)
echo.
echo Done: dist\PSU Studio.exe
pause
