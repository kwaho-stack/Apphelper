@echo off
rem Windows 에서 단일 exe 빌드: dist\Apphelper.exe
python -m pip install -r requirements-dev.txt || exit /b 1
python -m pytest -q || exit /b 1
python -m PyInstaller --noconfirm --clean apphelper.spec || exit /b 1
echo.
echo 완료: dist\Apphelper.exe
