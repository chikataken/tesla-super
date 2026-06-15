@echo off
REM One-command build: PyInstaller standalone exe -> Inno Setup installer.
REM Produces:
REM   dist\ShipmentCreator\ShipmentCreator.exe   (the portable app folder)
REM   installer\ShipmentCreatorSetup.exe         (the double-click installer)
cd /d "%~dp0"

if not exist ".venv\" (
  echo No .venv found. Run web.bat once first to create it.
  exit /b 1
)
call .venv\Scripts\activate.bat

echo === Installing build dependencies ===
pip install -r requirements.txt -q
pip install pyinstaller -q

echo === Building the app (PyInstaller) ===
pyinstaller shipment_creator.spec --noconfirm
if errorlevel 1 ( echo PyInstaller build FAILED. & exit /b 1 )

echo === Building the installer (Inno Setup) ===
where ISCC >nul 2>nul
if errorlevel 1 (
  echo.
  echo Inno Setup ^(ISCC.exe^) is not on PATH.
  echo Install it from https://jrsoftware.org/isdl.php  then re-run build.bat,
  echo   OR just zip and share the folder  dist\ShipmentCreator\  as a portable app.
  exit /b 0
)
ISCC installer.iss
if errorlevel 1 ( echo Installer build FAILED. & exit /b 1 )

echo.
echo === DONE ===
echo   Portable app : dist\ShipmentCreator\
echo   Installer    : installer\ShipmentCreatorSetup.exe
