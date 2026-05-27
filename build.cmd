@echo off
REM Build PortWho into a single exe (output: dist\PortWho.exe)
setlocal
where pyinstaller >nul 2>&1
if errorlevel 1 (
  echo PyInstaller not found. Installing...
  python -m pip install pyinstaller || goto :err
)
pyinstaller --noconfirm --onefile --windowed --name PortWho portwho.py || goto :err
echo.
echo Done: dist\PortWho.exe
goto :eof
:err
echo Build failed
exit /b 1
