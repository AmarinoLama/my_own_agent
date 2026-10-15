#!/usr/bin/env bash
# Lanzador del agente en Ubuntu/Linux.
#
#   ./scripts/agente.sh                 # chat por consola
#   ./scripts/agente.sh --voz           # modo voz con el orbe
#   ./scripts/agente.sh --help          # ayuda
#
# Usa el entorno virtual del proyecto (.venv) si existe; si no, python3 del sistema.
set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

if [ -x "$RAIZ/.venv/bin/python" ]; then
  PYTHON="$RAIZ/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
else
  echo "No encuentro Python 3. Instálalo con:  sudo apt install python3" >&2
  exit 1
fi

exec "$PYTHON" "$RAIZ/main.py" "$@"
