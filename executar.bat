@echo off
cd /d %~dp0
if not exist .venv (
  py -m venv .venv
  call .venv\Scripts\activate
  python -m pip install -r requirements.txt
) else (
  call .venv\Scripts\activate
)
python importar_excel.py
if errorlevel 1 goto fim
python main.py
:fim
pause
