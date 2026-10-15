#!/usr/bin/env bash
# Instala el agente en Ubuntu/Linux.
#
#   ./scripts/instalar_ubuntu.sh
#
# 1. Paquetes del sistema que Python no puede instalar por sí solo
#    (tk para la ventana, portaudio para el micrófono).
# 2. Entorno virtual del proyecto en .venv.
# 3. Dependencias de requirements.txt dentro de ese .venv.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

echo "==> Paquetes del sistema (puede pedir tu contraseña de sudo)"
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  echo "Hace falta root para instalar paquetes: vuelve a lanzarlo como root" >&2
  echo "o instala a mano: python3 python3-venv python3-tk portaudio19-dev" >&2
  exit 1
fi
$SUDO apt-get update
$SUDO apt-get install -y python3 python3-venv python3-pip python3-tk \
                         portaudio19-dev libasound2-dev

echo "==> Entorno virtual en .venv"
if [ ! -x "$RAIZ/.venv/bin/python" ]; then
  python3 -m venv "$RAIZ/.venv"
fi

echo "==> Dependencias de requirements.txt"
"$RAIZ/.venv/bin/python" -m pip install --upgrade pip
"$RAIZ/.venv/bin/python" -m pip install -r "$RAIZ/requirements.txt"

if [ ! -f "$RAIZ/.env" ]; then
  cp "$RAIZ/.env.example" "$RAIZ/.env"
  echo "==> Creado .env desde .env.example: rellena API_CHATGPT antes de usarlo."
fi

echo
echo "Listo. Arranca con:  ./scripts/agente.sh"
echo "Modo voz:            ./scripts/agente.sh --voz"
