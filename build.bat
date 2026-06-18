@echo off
REM ============================================================
REM  WindowWatch  -  build a standalone .exe
REM  Run this in a normal Windows command prompt (cmd or PowerShell)
REM  inside the folder that contains windowwatch.py
REM ============================================================

echo Installing dependencies...
pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo Building WindowWatch.exe ...
pyinstaller --onefile --noconsole --name WindowWatch ^
    --hidden-import win32gui ^
    --hidden-import win32con ^
    --hidden-import win32ui ^
    --hidden-import win32api ^
    --hidden-import win32process ^
    windowwatch.py
if errorlevel 1 goto :error

echo.
echo ============================================================
echo  Done.  Your app is at:  dist\WindowWatch.exe
echo  Double-click it to run.  No install needed.
echo ============================================================
goto :eof

:error
echo.
echo Build failed.  See the error above.
exit /b 1
