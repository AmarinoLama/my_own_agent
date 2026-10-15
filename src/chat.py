"""Chat por consola con tu agente: contesta y además recuerda.

    python src/chat.py
    python src/chat.py -o "recuerda que mi color favorito es el verde"
    python src/chat.py --memoria /ruta/a/mis-notas

Dentro del chat: /stats, /memoria, /reset y /salir.
"""

import argparse
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from chat_cli import configurar_salida  # noqa: E402
from cerebro import Agente, MODELO_DEFECTO, MODELO_RESPALDO  # noqa: E402

AZUL, GRIS, RESET = "\033[36m", "\033[90m", "\033[0m"
COMANDOS = "/stats  resumen   /memoria  lo que recuerda   /reset  borrar la conversación   /salir"


def main() -> None:
    parser = argparse.ArgumentParser(description="Habla con tu agente (con memoria).")
    parser.add_argument("-m", "--modelo", default=MODELO_DEFECTO, help="Modelo a usar")
    parser.add_argument("-o", "--once", help="Un solo mensaje y sale")
    parser.add_argument("--memoria", default=None, help="Carpeta de notas (por defecto memoria)")
    parser.add_argument("--nombre", default=None, help="Nombre del agente")
    parser.add_argument("--no-fallback", action="store_true",
                        help="No cambiar de modelo si el elegido no está disponible")
    args = parser.parse_args()

    configurar_salida()

    agente = Agente(
        modelo=args.modelo,
        memoria=args.memoria,
        nombre=args.nombre,
        respaldo=None if args.no_fallback else MODELO_RESPALDO,
    )

    if args.once:
        try:
            metricas = agente.preguntar(args.once)
            print(f"{GRIS}  [{metricas.resumen_corto()}]{RESET}")
        finally:
            agente.cerrar_sesion()
            agente.resumen()
        return

    print(f"{AZUL}{agente.nombre}{RESET} · modelo {agente.modelo}")
    print(f"{GRIS}Memoria: {agente.memoria.carpeta} ({len(agente.memoria.nombres())} notas)")
    print(f"{COMANDOS}{RESET}\n")

    try:
        while True:
            try:
                entrada = input("tú> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not entrada:
                continue

            if entrada.startswith("/"):
                if entrada in ("/salir", "/exit", "/quit"):
                    break
                if entrada == "/stats":
                    agente.resumen()
                    continue
                if entrada == "/memoria":
                    print(f"{GRIS}{agente.memoria.indice()}{RESET}")
                    continue
                if entrada == "/reset":
                    agente.nueva_conversacion()
                    print(f"{GRIS}Conversación archivada y borrada (la memoria sigue intacta).{RESET}")
                    continue

                print(f"{GRIS}Comandos: {COMANDOS}{RESET}")
                continue

            metricas = agente.preguntar(entrada)
            print(f"{GRIS}  [{metricas.resumen_corto()}]{RESET}")
    finally:
        agente.cerrar_sesion()
        agente.resumen()


if __name__ == "__main__":
    main()
