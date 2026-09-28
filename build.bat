@echo off
rem Builds dist\clip2vtf.exe (one file) and runs its self-test.
cd /d "%~dp0"
py -3 -m pip install -r requirements.txt pyinstaller || exit /b 1
py -3 check_translations.py || exit /b 1
py -3 -m PyInstaller --noconfirm clip2vtf.spec || exit /b 1
start /wait "" dist\clip2vtf.exe --selftest dist\selftest.txt
type dist\selftest.txt
