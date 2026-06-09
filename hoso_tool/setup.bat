@echo off
REM Cai dat nhanh tool tren may moi (Windows). Chay: setup.bat
cd /d "%~dp0\.."

where python >nul 2>nul
if errorlevel 1 ( echo Thieu Python. Cai Python 3 tu python.org roi chay lai. & pause & exit /b 1 )

echo ==^> Tao virtualenv .venv
python -m venv .venv

echo ==^> Cai thu vien Python
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r hoso_tool\requirements.txt

echo.
echo LUU Y: can cai poppler cho Windows (pdfinfo/pdftoppm/pdfunite):
echo   - Tai: https://github.com/oschwartz10612/poppler-windows/releases
echo   - Giai nen, them thu muc ...\poppler\Library\bin vao PATH
echo.
echo XONG! Chay giao dien bang:
echo   .venv\Scripts\streamlit run hoso_tool\app.py
pause
