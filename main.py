"""Punto de entrada único del proyecto: chat por consola o modo voz.

    python main.py          # chat por consola
    python main.py --voz    # hablar con el orbe flotante
    python main.py --help   # ayuda

El resto de módulos vive en este mismo directorio.
"""

import sys

from chat_cli import main as main_chat


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
