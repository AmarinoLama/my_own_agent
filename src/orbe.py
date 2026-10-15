"""La barra notch con tu agente de memoria detrás.

    python src/orbe.py                   # hablar por micrófono
    python src/orbe.py --texto           # escribir en la consola
    python src/orbe.py --acceso-directo  # icono "Agente" en el escritorio

El orbe, el oído y la voz se implementan en voz.py; aquí solo se le conecta el
agente con memoria.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
RAIZ = SRC.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import voz  # noqa: E402


def main() -> None:
    parser = voz.crear_parser("Habla con tu agente usando la barra de arriba.")
    parser.add_argument("--memoria", default=None, help="Carpeta de notas del agente")
    parser.add_argument("--nombre", default=None, help="Nombre del agente")
    args = parser.parse_args()

    voz.configurar_salida()

    if args.acceso_directo:
        ruta = voz.crear_acceso_directo(
            script=SRC / "orbe.py",
            nombre="Agente",
            descripcion="Hablar con tu agente (orbe)",
            icono=RAIZ / "assets" / "orbe-agente.ico",
            centro=(150, 255, 190),   # verde, para no confundirlo con el orbe azul
            borde=(18, 120, 70),
            tecla=args.tecla or "CTRL+ALT+A",
        )
        voz.log(f"{voz.AZUL}Acceso directo creado:{voz.RESET} {ruta}")
        return

    # El micrófono se resuelve igual que en el modo voz normal
    micro_pedido = args.micro if args.micro is not None else voz.leer_config().get("micro")
    indice_micro = voz.resolver_micro(micro_pedido)

    if voz.atender_palmadas(args, indice_micro):
        return

    if voz.atender_microfono(args, indice_micro):
        return

    def preparar_agente():
        # El orbe ya debe estar visible antes de cargar memoria/configuración
        # del agente: pythonw no tiene consola donde enseñar una excepción.
        from cerebro import Agente

        return Agente(
            modelo=args.modelo,
            memoria=args.memoria,
            nombre=args.nombre,
            respaldo=None if args.no_fallback else voz.MODELO_RESPALDO,
            usar_color=False,
        )

    voz.arrancar(None, args, indice_micro, preparar_chat=preparar_agente)


if __name__ == "__main__":
    main()
