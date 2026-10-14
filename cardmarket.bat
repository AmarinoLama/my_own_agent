@echo off
setlocal
cd /d "%~dp0"
if not exist "cardmarket-angular\node_modules\@angular\cli\bin\ng.js" (
  echo Instalando las dependencias locales de Angular...
  call npm install --prefix cardmarket-angular --no-audit --no-fund
  if errorlevel 1 goto error
)
if not exist "cardmarket-angular\dist\cardmarket-angular\browser\index.html" (
  echo Construyendo la interfaz Angular...
  call npm run build --prefix cardmarket-angular
  if errorlevel 1 goto error
)
echo Abriendo Cardpilot en http://127.0.0.1:8765/
python cardmarket.py
if errorlevel 1 goto error
goto end
:error
echo.
echo No se pudo iniciar Cardpilot. Comprueba que Node.js, npm y Python estan instalados.
pause
:end
endlocal
