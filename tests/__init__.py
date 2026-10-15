"""Pruebas del agente.

Este paquete añade `src/` al path para que los tests funcionen igual con
pytest (`pytest`) y con el descubridor de la biblioteca estándar
(`python -m unittest discover`), sin configuración extra.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
