@echo off
rem Lanzador del agente en Windows.
rem
rem   scripts\agente.bat            -> chat por consola
rem   scripts\agente.bat --voz      -> modo voz con el orbe
rem   scripts\agente.bat --help     -> ayuda
rem
rem Usa el entorno virtual del proyecto (.venv) si existe; si no, python del PATH.
setlocal
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

"%PY%" main.py %*
if errorlevel 1 (
  echo.
  echo No se pudo arrancar el agente. Comprueba que Python este instalado y que
  echo las dependencias esten puestas:  pip install -r requirements.txt
  pause
)
endlocal
