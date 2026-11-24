@echo off
:: Start the PrintingShopSystem server using the venv Python (which has pytesseract + Pillow for OCR).
:: Running with the system Python instead would cause "OCR Status: Failed" on every receipt scan.
cd /d "%~dp0"
call venv\Scripts\activate.bat
venv\Scripts\python.exe -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
