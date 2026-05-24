@echo off
echo === FlowerGraph: создание виртуального окружения ===
python -m venv .venv
call .venv\Scripts\activate.bat
pip install --upgrade pip
pip install -r requirements.txt
echo === Готово ===
pause
