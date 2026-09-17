"""Chat por consola contra la API de ChatGPT (compatible con OpenAI).

Usa la variable de entorno API_CHATGPT que está en el .env del proyecto.
Modelo por defecto: mistralai/mistral-large-2512. Si tu key no tuviera permiso
para el modelo elegido (error 403, requiere plan de pago), el chat avisa y sigue
con un modelo de respaldo gratuito para que puedas seguir hablando.

    python chat_cli.py                                  # chat interactivo
    python chat_cli.py --once "¿Quién es Ibai?"         # una consulta + métricas
    python chat_cli.py --models                         # lista de modelos disponibles
    python chat_cli.py --bench                          # compara velocidad entre modelos
    python chat_cli.py -m qwen/qwen3.7-plus:free        # elegir modelo

Comandos dentro del chat:
    /stats   -> resumen de rendimiento de la sesión
    /reset   -> borra el historial de la conversación
    /model   -> cambia el modelo en caliente
    /help    -> ayuda
    /salir   -> termina la sesión
"""

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

# Cargar el .env de la raíz del proyecto
RAIZ = Path(__file__).resolve().parent
load_dotenv(RAIZ / ".env")

# Base URL y modelo por defecto (se pueden sobrescribir por .env o por argumentos)
BASE_URL_DEFECTO = os.getenv("API_CHATGPT_BASE_URL", "https://api.xkiro.com/v1")
MODELO_DEFECTO = os.getenv("API_CHATGPT_MODEL", "mistralai/mistral-large-2512")

# Si el modelo elegido no está disponible (p. ej. requiere plan de pago), el chat
# reintenta con este y, si tampoco existe, con los :free que liste la API en ese
# momento (esos nombres rotan).
MODELO_RESPALDO = os.getenv("API_CHATGPT_FALLBACK", "qwen/qwen3.7-plus:free")

# Modelos que se comparan en --bench (cámbialos por los que quieras medir)
MODELOS_BENCH = [
    "deepseek/deepseek-v4.1-flash:free",
    "minimax/minimax-m2.5:free",
    "minimax/minimax-m2.5-highspeed:free",
    "qwen/qwen3.7-plus:free",
    "qwen/qwen3.8-max:free",
    "mistralai/ministral-8b",
]

# Colores ANSI sencillos, se desactivan con --no-color
AZUL = "\033[36m"
VERDE = "\033[32m"
GRIS = "\033[90m"
ROJO = "\033[31m"
RESET = "\033[0m"


def color(texto: str, codigo: str, activo: bool) -> str:
    """Envuelve un texto en un color ANSI si están activados."""
    return f"{codigo}{texto}{RESET}" if activo else texto


def configurar_salida() -> None:
    """Fuerza UTF-8 en la consola: si no, un emoji del modelo rompe el chat en Windows."""
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")


@dataclass
class Metricas:
    """Rendimiento de una única petición."""

    texto: str = ""
    ttft: float | None = None      # tiempo hasta el primer token
    total: float = 0.0             # tiempo total de la petición
    tokens_prompt: int | None = None
    tokens_respuesta: int | None = None
    error: str | None = None

    @property
    def estimado(self) -> bool:
        return self.tokens_respuesta is not None and self.tokens_prompt is None

    @property
    def tokens_por_segundo(self) -> float | None:
        """Velocidad de generación, sin contar el tiempo del primer token."""
        if not self.tokens_respuesta:
            return None
        generacion = self.total - (self.ttft or 0)
        return self.tokens_respuesta / generacion if generacion > 0 else None

    def resumen_corto(self) -> str:
        """Línea compacta con las métricas, para imprimir bajo cada respuesta."""
        tokens = (
            f"{self.tokens_prompt} + {self.tokens_respuesta}" if not self.estimado
            else f"n/d ~ {self.tokens_respuesta} (estimado)"
        )
        velocidad = self.tokens_por_segundo
        return " | ".join(
            [
                f"1er token: {self.ttft * 1000:.0f} ms" if self.ttft is not None else "sin stream",
                f"total: {self.total:.2f} s",
                f"tokens: {tokens}",
                f"velocidad: {velocidad:.1f} tok/s" if velocidad else "velocidad: n/d",
            ]
        )


def promediar(metricas: list[Metricas]) -> Metricas:
    """Combina varias ejecuciones en una sola media (más fiable que una única petición)."""
    utiles = [m for m in metricas if m.error is None]
    if not utiles:
        return metricas[0]

    # Solo promediamos el primer token de las ejecuciones que lo midieron
    con_ttft = [m.ttft for m in utiles if m.ttft is not None]

    return Metricas(
        texto=utiles[0].texto,
        ttft=sum(con_ttft) / len(con_ttft) if con_ttft else None,
        total=sum(m.total for m in utiles) / len(utiles),
        tokens_prompt=round(sum(m.tokens_prompt or 0 for m in utiles) / len(utiles)),
        tokens_respuesta=round(sum(m.tokens_respuesta or 0 for m in utiles) / len(utiles)),
    )


def texto_metrica(etiqueta: str, valor: str | None) -> str:
    """Formatea una línea de métrica alineada para el resumen final."""
    return f"  {etiqueta + ':':<19}{valor or 'n/d'}"


def es_error_de_modelo(error: Exception) -> bool:
    """Detecta si el fallo es porque el modelo no está disponible con esta key."""
    mensaje = str(error).lower()
    return any(clave in mensaje for clave in ("permission_denied", "not_found", "403", "404"))


def error_legible(error: Exception) -> str:
    """Traduce los errores de la API a algo accionable."""
    mensaje = str(error)

    if "403" in mensaje or "permission" in mensaje.lower():
        return (
            f"{mensaje}\n"
            "  -> Ese modelo requiere plan de pago. Prueba uno gratuito con:\n"
            "     python chat_cli.py --models    (busca los que acaban en :free)"
        )
    if "401" in mensaje or "invalid or disabled clientapikey" in mensaje.lower():
        return (
            f"{mensaje}\n"
            "  -> La clave API_CHATGPT es inválida o está desactivada. "
            "Genera una clave nueva en api.xkiro.com y reemplázala en el archivo .env."
        )
    if "Connection" in type(error).__name__:
        return f"{mensaje}\n  -> No se pudo conectar con {BASE_URL_DEFECTO}."

    return mensaje


class ChatCLI:
    """Chat interactivo con métricas de rendimiento por turno."""

    def __init__(
        self,
        modelo: str = MODELO_DEFECTO,
        system: str | None = None,
        usar_color: bool = True,
        stream: bool = True,
        respaldo: str | None = MODELO_RESPALDO,
    ):
        # API_CHATGPT es la variable del proyecto; OPENAI_API_KEY sirve de alternativa
        api_key = os.getenv("API_CHATGPT") or os.getenv("OPENAI_API_KEY")

        if not api_key:
            raise SystemExit(
                "Falta la variable API_CHATGPT. "
                f"Añádela al archivo {RAIZ / '.env'} como:\n  API_CHATGPT=sk-..."
            )

        self.client = OpenAI(api_key=api_key, base_url=BASE_URL_DEFECTO)
        self.modelo = modelo
        self.respaldo = respaldo
        self.candidatos_respaldo: list[str] | None = None  # se resuelve a demanda
        self.modelos_probados: set[str] = {modelo}
        self.color = usar_color
        self.stream = stream

        # Historial de la conversación en formato OpenAI
        self.historial: list[dict] = []
        if system:
            self.historial.append({"role": "system", "content": system})

        # Acumuladores de rendimiento de la sesión
        self.turnos = 0
        self.tokens_prompt = 0
        self.tokens_respuesta = 0
        self.tiempos_primer_token: list[float] = []
        self.tiempos_totales: list[float] = []
        self.velocidades: list[float] = []

    def _siguiente_respaldo(self) -> str | None:
        """Modelo de respaldo no probado aún, o None si ya no queda ninguno."""
        if not self.respaldo:
            return None

        # Los modelos gratuitos disponibles cambian con el tiempo: los pedimos a la API
        if self.candidatos_respaldo is None:
            try:
                libres = [m.id for m in self.client.models.list().data if m.id.endswith(":free")]
            except Exception:
                libres = []

            self.candidatos_respaldo = [self.respaldo] + [m for m in libres if m != self.respaldo]

        return next((m for m in self.candidatos_respaldo if m not in self.modelos_probados), None)

    def _lanzar(self, stream_options: dict | None):
        """Hace la petición a chat/completions."""
        parametros = {
            "model": self.modelo,
            "messages": self.historial,
        }

        if self.stream:
            parametros["stream"] = True
            parametros["stream_options"] = stream_options

        return self.client.chat.completions.create(**parametros)

    def preguntar(self, texto: str, imprimir: bool = True, registrar: bool = True) -> Metricas:
        """Envía un mensaje y devuelve/ muestra sus métricas de rendimiento."""
        self.historial.append({"role": "user", "content": texto})
        metricas = Metricas()
        inicio = time.perf_counter()

        try:
            if self.stream:
                # Algunos proxies no soportan stream_options: reintentamos sin él
                try:
                    respuesta = self._lanzar({"include_usage": True})
                except Exception:
                    respuesta = self._lanzar(None)

                if imprimir:
                    print(color("\nassistant> ", VERDE, self.color), end="", flush=True)

                for trozo in respuesta:
                    # El último chunk (con stream_options) trae el conteo de tokens
                    if getattr(trozo, "usage", None):
                        metricas.tokens_prompt = trozo.usage.prompt_tokens
                        metricas.tokens_respuesta = trozo.usage.completion_tokens

                    if not trozo.choices:
                        continue

                    contenido = trozo.choices[0].delta.content
                    if not contenido:
                        continue

                    if metricas.ttft is None:
                        metricas.ttft = time.perf_counter() - inicio

                    metricas.texto += contenido
                    if imprimir:
                        print(contenido, end="", flush=True)
            else:
                # Sin stream medimos la latencia completa de la petición
                respuesta = self._lanzar(None)
                metricas.texto = respuesta.choices[0].message.content or ""
                if respuesta.usage:
                    metricas.tokens_prompt = respuesta.usage.prompt_tokens
                    metricas.tokens_respuesta = respuesta.usage.completion_tokens
                if imprimir:
                    print(color("\nassistant> ", VERDE, self.color) + metricas.texto)

        except Exception as error:
            # No dejamos el historial con un mensaje sin respuesta
            self.historial.pop()

            # Si el modelo elegido no está disponible, reintentamos con el respaldo
            if es_error_de_modelo(error):
                candidato = self._siguiente_respaldo()

                if candidato:
                    # Marcamos el candidato para no reintentar el mismo modelo en bucle
                    self.modelos_probados.add(candidato)
                    print(
                        color(
                            f"\n[!] {self.modelo} no está disponible con tu key → {candidato}",
                            ROJO,
                            self.color,
                        )
                    )
                    self.modelo = candidato
                    return self.preguntar(texto, imprimir=imprimir, registrar=registrar)

            metricas.error = error_legible(error)
            metricas.total = time.perf_counter() - inicio
            if imprimir:
                print(color(f"\nError: {metricas.error}", ROJO, self.color))
            return metricas

        metricas.total = time.perf_counter() - inicio

        # Si la API no devuelve usage, estimamos ~4 caracteres por token
        if metricas.tokens_respuesta is None:
            metricas.tokens_respuesta = max(1, len(metricas.texto) // 4)

        self.historial.append({"role": "assistant", "content": metricas.texto})

        if imprimir:
            print()
            print(color(f"  [{metricas.resumen_corto()}]", GRIS, self.color))

        if registrar:
            self._acumular(metricas)

        return metricas

    def _acumular(self, metricas: Metricas) -> None:
        """Suma las métricas del turno a los totales de la sesión."""
        self.turnos += 1
        self.tokens_prompt += metricas.tokens_prompt or 0
        self.tokens_respuesta += metricas.tokens_respuesta or 0
        self.tiempos_totales.append(metricas.total)

        if metricas.ttft is not None:
            self.tiempos_primer_token.append(metricas.ttft)

        velocidad = metricas.tokens_por_segundo
        if velocidad:
            self.velocidades.append(velocidad)

    def resumen(self) -> None:
        """Muestra las estadísticas acumuladas de la sesión."""
        if not self.turnos:
            print(color("Todavía no hay turnos que medir.", GRIS, self.color))
            return

        def promedio(valores: list[float]) -> float | None:
            return sum(valores) / len(valores) if valores else None

        ttft = promedio(self.tiempos_primer_token)
        total = promedio(self.tiempos_totales)
        velocidad = promedio(self.velocidades)

        print(color("\n── Rendimiento de la sesión ─────────────", AZUL, self.color))
        print(f"  Modelo:            {self.modelo}")
        print(f"  Turnos:            {self.turnos}")
        print(texto_metrica("1er token (medio)", f"{ttft * 1000:.0f} ms" if ttft is not None else None))
        print(texto_metrica("Latencia media", f"{total:.2f} s" if total else None))
        print(texto_metrica("Velocidad media", f"{velocidad:.1f} tok/s" if velocidad else None))
        print(f"  Tokens prompt:     {self.tokens_prompt}")
        print(f"  Tokens respuesta:  {self.tokens_respuesta}")
        print(f"  Tokens totales:    {self.tokens_prompt + self.tokens_respuesta}")
        print(color("──────────────────────────────────────────\n", AZUL, self.color))

    def ayuda(self) -> None:
        print(
            color(
                "/stats  resumen de rendimiento   /reset  borrar historial\n"
                "/model  cambiar de modelo        /salir  terminar",
                GRIS,
                self.color,
            )
        )

    def loop(self) -> None:
        """Bucle principal del chat."""
        print(color(f"Chat con {self.modelo} en {BASE_URL_DEFECTO}", AZUL, self.color))
        print(color("Escribe /help para ver los comandos. Ctrl+C para salir.\n", GRIS, self.color))

        while True:
            try:
                entrada = input(color("tú> ", AZUL, self.color)).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not entrada:
                continue

            # Comandos que empiezan por "/"
            if entrada.startswith("/"):
                comando, _, argumento = entrada.partition(" ")
                comando = comando.lower()

                if comando in ("/salir", "/exit", "/quit"):
                    break
                if comando == "/stats":
                    self.resumen()
                    continue
                if comando == "/reset":
                    self.historial = [m for m in self.historial if m["role"] == "system"]
                    self.turnos = 0
                    self.tokens_prompt = 0
                    self.tokens_respuesta = 0
                    self.tiempos_primer_token.clear()
                    self.tiempos_totales.clear()
                    self.velocidades.clear()
                    print(color("Historial y métricas borrados.", GRIS, self.color))
                    continue
                if comando == "/model":
                    if argumento.strip():
                        self.modelo = argumento.strip()
                        # Un modelo nuevo vuelve a tener derecho a su respaldo
                        self.modelos_probados = {self.modelo}
                        print(color(f"Modelo: {self.modelo}", GRIS, self.color))
                    else:
                        print(color(f"Modelo actual: {self.modelo}", GRIS, self.color))
                    continue
                if comando == "/help":
                    self.ayuda()
                    continue

                print(color(f"Comando desconocido: {comando}", ROJO, self.color))
                continue

            self.preguntar(entrada)

        self.resumen()


def listar_modelos(usar_color: bool = True) -> None:
    """Lista los modelos que acepta la API, marcando los gratuitos."""
    chat = ChatCLI(usar_color=usar_color)
    modelos = sorted(m.id for m in chat.client.models.list().data)

    print(color(f"{len(modelos)} modelos disponibles:", AZUL, usar_color))
    for modelo in modelos:
        etiqueta = color("  [free]", VERDE, usar_color) if modelo.endswith(":free") else ""
        print(f" - {modelo}{etiqueta}")

    print(color("\nLos que no llevan [free] suelen pedir plan de pago (error 403).", GRIS, usar_color))


def bench(prompt: str, modelos: list[str], usar_color: bool = True, stream: bool = True,
          repeticiones: int = 1) -> None:
    """Mide el mismo prompt en varios modelos y saca una tabla comparativa."""
    print(color(f"\nBenchmark con {len(modelos)} modelos x {repeticiones} peticiones", AZUL, usar_color))
    print(color(f'Prompt: "{prompt}"', GRIS, usar_color))
    print(color("Midiendo... (cada modelo hace peticiones reales)\n", GRIS, usar_color))

    filas = []
    for modelo in modelos:
        # Sin respaldo: en el benchmark queremos el resultado real de cada modelo
        chat = ChatCLI(modelo=modelo, usar_color=usar_color, stream=stream, respaldo=None)
        ejecuciones = []

        for numero in range(repeticiones):
            metricas = chat.preguntar(prompt, imprimir=False, registrar=False)
            chat.historial = []  # cada repetición mide el mismo prompt desde cero
            if metricas.error:
                print(color(f"  {modelo}: error -> {metricas.error.splitlines()[0]}", ROJO, usar_color))
                ejecuciones = []
                break
            ejecuciones.append(metricas)
            if repeticiones > 1:
                print(color(f"  {modelo} [{numero + 1}/{repeticiones}]: {metricas.resumen_corto()}", GRIS, usar_color))

        if ejecuciones:
            media = promediar(ejecuciones)
            filas.append((modelo, media))
            print(color(f"  {modelo}: {media.resumen_corto()}", GRIS, usar_color))
        else:
            filas.append((modelo, None))

    # Tabla ordenada por velocidad de generación
    anchura = max([len(m) for m, _ in filas] + [len("Modelo")]) + 2
    print(color("\n── Comparativa ──────────────────────────────", AZUL, usar_color))
    cabecera = f"{'Modelo':<{anchura}}{'1er token':>11}{'Total':>9}{'Tokens':>9}{'Velocidad':>15}"
    print(f"  {cabecera}")

    for modelo, metricas in sorted(
        filas,
        key=lambda fila: fila[1].tokens_por_segundo or 0 if fila[1] else 0,
        reverse=True,
    ):
        if metricas is None:
            print(f"  {modelo:<{anchura}}{'error':>11}")
            continue

        velocidad = metricas.tokens_por_segundo
        ttft = f"{metricas.ttft * 1000:.0f} ms" if metricas.ttft is not None else "n/d"
        print(
            f"  {modelo:<{anchura}}"
            f"{ttft:>11}"
            f"{f'{metricas.total:.2f} s':>9}"
            f"{metricas.tokens_respuesta:>9}"
            f"{(f'{velocidad:.1f} tok/s' if velocidad else 'n/d'):>15}"
        )

    print(color("─────────────────────────────────────────────\n", AZUL, usar_color))


def main() -> None:
    # Antes de parsear: argparse escribe la ayuda por stdout y también lleva acentos
    configurar_salida()

    parser = argparse.ArgumentParser(description="Chat por consola contra la API de ChatGPT.")
    parser.add_argument("-o", "--once", help="Envía un único mensaje y termina")
    parser.add_argument("-m", "--model", default=MODELO_DEFECTO, help="Modelo a usar")
    parser.add_argument("-s", "--system", default=None, help="Mensaje de sistema inicial")
    parser.add_argument("--models", action="store_true", help="Lista los modelos disponibles")
    parser.add_argument("--bench", nargs="?", const="Explica en tres frases qué es un asistente de inteligencia artificial.",
                        help="Compara modelos con este prompt")
    parser.add_argument("--bench-models", help="Lista de modelos para --bench separados por comas")
    parser.add_argument("-r", "--runs", type=int, default=1,
                        help="Peticiones por modelo en --bench (se promedian)")
    parser.add_argument("--no-stream", action="store_true",
                        help="Sin streaming (mide la latencia completa de la petición)")
    parser.add_argument("--no-fallback", action="store_true",
                        help="No usar un modelo de respaldo si el elegido no está disponible")
    parser.add_argument("--no-color", action="store_true", help="Desactiva los colores")
    args = parser.parse_args()

    usar_color = not args.no_color

    # Los archivos ANSI en Windows necesitan que la consola los soporte
    if os.name == "nt" and usar_color:
        os.system("")

    try:
        if args.models:
            listar_modelos(usar_color)
        elif args.bench:
            modelos = (
                [m.strip() for m in args.bench_models.split(",") if m.strip()]
                if args.bench_models else MODELOS_BENCH
            )
            bench(args.bench, modelos, usar_color, stream=not args.no_stream,
                  repeticiones=max(1, args.runs))
        else:
            respaldo = None if args.no_fallback else MODELO_RESPALDO
            chat = ChatCLI(args.model, args.system, usar_color,
                           stream=not args.no_stream, respaldo=respaldo)

            if args.once:
                metricas = chat.preguntar(args.once)
                chat.resumen()
                # Si la única petición falló, el script termina con error
                if metricas.error:
                    sys.exit(1)
            else:
                chat.loop()
    except SystemExit as error:
        print(color(str(error), ROJO, usar_color))
        sys.exit(1)


if __name__ == "__main__":
    main()
