@echo off
rem Abre el planificador con el servidor local: precios, disponibilidad y fotos de los alojamientos.
cd /d "%~dp0"
python src\app_server.py
pause
