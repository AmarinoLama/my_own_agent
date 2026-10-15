"""Punto de entrada único del proyecto: chat por consola o modo voz.

    python main.py          # chat por consola
    python main.py --voz    # hablar con el orbe
    python main.py --help   # ayuda

Los módulos del agente viven en `src/`; este lanzador se limita a poner esa
carpeta en el path para que los imports funcionen igual en Windows y en Ubuntu.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chat_cli import main as main_chat  # noqa: E402


def lanzar() -> None:
    """Abre el chat normal o el orbe si se indica --voz."""
    if "--voz" in sys.argv[1:]:
        sys.argv.remove("--voz")
        from voz import main as main_voz

        main_voz()
    else:
        main_chat()


if __name__ == "__main__":
    lanzar()
