@echo off
setlocal
cd /d "%~dp0"
echo ============================================
echo  PhoneLauncher - build exe
echo  folder: %CD%
echo ============================================
echo.

if not exist "phone_launcher.py" (
  echo [ERROR] phone_launcher.py not found in this folder.
  echo         Please EXTRACT the zip first, then run build_exe.bat from the extracted folder.
  goto :fail
)

rem --- find python (py launcher first, then python) ---
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [ERROR] Python not found. Install Python 3.8+ and check "Add to PATH".
  goto :fail
)
echo [1/4] Python:
%PY% --version
if errorlevel 1 (
  echo [ERROR] Python failed to run. If Windows opened the Microsoft Store, Python is not really installed.
  goto :fail
)

if not exist ".venv\Scripts\python.exe" (
  echo [2/4] Creating virtual env .venv ...
  %PY% -m venv .venv
  if errorlevel 1 goto :fail
) else (
  echo [2/4] Using existing .venv
)

echo [3/4] Installing PySide6 + PyInstaller ^(takes a few minutes^) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto :fail

echo [4/4] Building exe ...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed --name PhoneLauncher phone_launcher.py
if errorlevel 1 goto :fail

echo.
echo ============================================
echo  DONE:  %CD%\dist\PhoneLauncher.exe
echo  Put the "scrcpy" folder next to the exe.
echo ============================================
pause
exit /b 0

:fail
echo.
echo ============ BUILD FAILED ============
echo Please send a screenshot of this window.
pause
exit /b 1
