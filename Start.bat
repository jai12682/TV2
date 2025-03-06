@echo off
cd C:\Python Projects\TV2
call .venv\Scripts\activate
start python main_script.py
ngrok http 5000