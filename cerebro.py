"""El cerebro del agente: un modelo con memoria persistente en notas de texto.

La memoria son ficheros .md dentro de `memoria/` que el propio agente lee
y escribe con herramientas (function calling). Por eso recuerda entre sesiones, y
por eso puedes abrir sus recuerdos con cualquier editor y corregirlos a mano.

Usa la misma API y la misma clave (API_CHATGPT) que el resto del proyecto.
"""

import ctypes
import ctypes.wintypes
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

RAIZ = Path(__file__).resolve().parent  # carpeta del proyecto
CARPETA = RAIZ                         # alias de compatibilidad

# Cargamos la configuración desde el único directorio del proyecto.

from chat_cli import Metricas, configurar_salida, error_legible, es_error_de_modelo  # noqa: E402

# Sin esto, un carácter que la consola no sepa escribir (→, ✔, emojis) haría
# fallar los prints y las trazas desaparecerían sin dejar rastro.
configurar_salida()

load_dotenv(RAIZ / ".env")

AZUL, VERDE, GRIS, ROJO, RESET = "\033[36m", "\033[32m", "\033[90m", "\033[31m", "\033[0m"


def log(*partes) -> None:
    """print seguro: con pythonw (sin consola) lo apunta en un fichero.

    Si algo falla al escribirlo, se apunta igualmente en agente.log: perder una
    traza en silencio esconde justo los problemas que interesa ver.
    """
    texto = " ".join(str(p) for p in partes)

    if sys.stdout is not None:
        try:
            print(texto, flush=True)
            return
        except Exception:
            pass

    try:
        with open(RAIZ / "agente.log", "a", encoding="utf-8") as fichero:
            fichero.write(texto + "\n")
    except OSError:
        pass

BASE_URL_DEFECTO = os.getenv("API_CHATGPT_BASE_URL", "https://api.xkiro.com/v1")
MODELO_DEFECTO = os.getenv("API_CHATGPT_MODEL", "mistralai/mistral-large-2512")
MODELO_RESPALDO = os.getenv("API_CHATGPT_FALLBACK", "qwen/qwen3.7-plus:free")
NOMBRE_DEFECTO = os.getenv("AGENTE_NOMBRE", "Agente")
IDIOMA_DEFECTO = os.getenv("AGENTE_IDIOMA", "es-ES")
MEMORIA_DEFECTO = Path(os.getenv("AGENTE_MEMORIA") or CARPETA / "memoria")

# Cuántas vueltas de herramientas puede dar el modelo en un mismo turno
MAX_RONDAS = 6
ESCRITORIOS = CARPETA / "escritorios"


def _ventanas_visibles() -> list[dict]:
    """Obtiene ventanas principales visibles de Windows y sus rectángulos."""
    if os.name != "nt":
        return []

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    ventanas = []
    propia = os.getpid()

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, 4):  # GW_OWNER
            return True
        longitud = user32.GetWindowTextLengthW(hwnd)
        if longitud <= 0:
            return True
        titulo = ctypes.create_unicode_buffer(longitud + 1)
        user32.GetWindowTextW(hwnd, titulo, longitud + 1)
        titulo = titulo.value.strip()
        if not titulo:
            return True

        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True

        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proceso = ""
        handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid.value)  # QUERY_INFORMATION | VM_READ
        if handle:
            try:
                tamaño = ctypes.wintypes.DWORD(32768)
                ruta = ctypes.create_unicode_buffer(tamaño.value)
                if kernel32.QueryFullProcessImageNameW(handle, 0, ruta, ctypes.byref(tamaño)):
                    proceso = ruta.value
            finally:
                kernel32.CloseHandle(handle)

        if pid.value != propia:
            ventanas.append({
                "hwnd": int(hwnd),
                "titulo": titulo,
                "pid": int(pid.value),
                "proceso": proceso,
                "x": int(rect.left), "y": int(rect.top),
                "ancho": int(rect.right - rect.left), "alto": int(rect.bottom - rect.top),
                "maximizada": bool(user32.IsZoomed(hwnd)),
                "minimizada": bool(user32.IsIconic(hwnd)),
            })
        return True

    callback_tipo = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    user32.EnumWindows(callback_tipo(callback), 0)
    return ventanas


def _guardar_escritorio(nombre: str) -> str:
    """Guarda el estado actual de las ventanas en un JSON legible."""
    limpio = re.sub(r"[^\\w áéíóúüñ-]", "", str(nombre).strip().lower())
    limpio = re.sub(r"\\s+", "-", limpio).strip("-")[:60]
    if not limpio:
        return "(indica un nombre para el escritorio)"
    ventanas = _ventanas_visibles()
    ESCRITORIOS.mkdir(parents=True, exist_ok=True)
    ruta = ESCRITORIOS / f"{limpio}.json"
    ruta.write_text(json.dumps({
        "nombre": nombre.strip(),
        "creado": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ventanas": ventanas,
        "nota": "Se guardan posiciones y tamaños. Las pestañas internas de Firefox no se pueden leer mediante la API de ventanas de Windows.",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"escritorio «{nombre.strip()}» guardado con {len(ventanas)} ventanas"


def _restaurar_escritorio(nombre: str) -> str:
    """Reabre aplicaciones registradas y coloca sus ventanas como estaban."""
    if os.name != "nt":
        return "(la restauración de escritorios solo está disponible en Windows)"
    limpio = re.sub(r"[^\\w áéíóúüñ-]", "", str(nombre).strip().lower())
    limpio = re.sub(r"\\s+", "-", limpio).strip("-")[:60]
    ruta = ESCRITORIOS / f"{limpio}.json"
    if not ruta.exists():
        disponibles = ", ".join(p.stem for p in ESCRITORIOS.glob("*.json")) if ESCRITORIOS.exists() else "ninguno"
        return f"(no existe ese escritorio; disponibles: {disponibles})"

    datos = json.loads(ruta.read_text(encoding="utf-8"))
    guardadas = datos.get("ventanas", [])
    lanzadas = 0
    for ventana in guardadas:
        proceso = ventana.get("proceso", "")
        if proceso and os.path.isfile(proceso) and proceso.lower().endswith(".exe"):
            try:
                subprocess.Popen([proceso], close_fds=True)
                lanzadas += 1
            except OSError:
                pass

    user32 = ctypes.windll.user32
    encontrados = []
    limite = time.time() + 8
    while time.time() < limite and len(encontrados) < len(guardadas):
        actuales = _ventanas_visibles()
        for guardada in guardadas:
            if guardada in encontrados:
                continue
            objetivo = os.path.basename(guardada.get("proceso", "")).lower()
            candidatos = [v for v in actuales if os.path.basename(v.get("proceso", "")).lower() == objetivo]
            if guardada.get("titulo"):
                con_titulo = [v for v in candidatos if guardada["titulo"].lower() in v["titulo"].lower()]
                candidatos = con_titulo or candidatos
            if not candidatos:
                continue
            actual = candidatos[0]
            hwnd = actual["hwnd"]
            if guardada.get("maximizada"):
                user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
            elif guardada.get("minimizada"):
                user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            else:
                user32.SetWindowPos(
                    hwnd, 0, int(guardada.get("x", 0)), int(guardada.get("y", 0)),
                    int(guardada.get("ancho", 900)), int(guardada.get("alto", 700)),
                    0x0010 | 0x0004,  # SWP_NOACTIVATE | SWP_NOZORDER
                )
            encontrados.append(guardada)
        if len(encontrados) < len(guardadas):
            time.sleep(0.25)

    return f"escritorio «{datos.get('nombre', nombre)}» restaurado: {len(encontrados)}/{len(guardadas)} ventanas colocadas"


# Cuando digan esto, tienen que quedar apuntado sí o sí
PIDE_RECORDAR = re.compile(
    r"\b(recu[eé]rd\w*|apunt\w*|anot\w*|memoriz\w*|no lo olvides|ten en cuenta)\b",
    re.IGNORECASE,
)


def pide_recordar(texto: str) -> bool:
    """True si el mensaje pide claramente guardar algo en memoria."""
    return bool(PIDE_RECORDAR.search(texto))


class Memoria:
    """La memoria del agente: notas .md de texto plano, legibles y editables."""

    def __init__(self, carpeta: Path | str = MEMORIA_DEFECTO):
        self.carpeta = Path(carpeta)
        self.carpeta.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _nombre_limpio(nombre: str) -> str:
        """Convierte lo que diga el modelo en un nombre de fichero seguro."""
        # El modelo suele decir «proyectos.md»: la extensión la ponemos nosotros
        sin_extension = re.sub(r"\.md$", "", str(nombre).strip(), flags=re.IGNORECASE)
        limpio = re.sub(r"[^\w \-áéíóúüñ]", "", sin_extension.lower())
        limpio = re.sub(r"[\s_]+", "-", limpio).strip("-")

        if not limpio:
            raise ValueError("el nombre de la nota no puede estar vacío")

        return limpio[:60]

    def ruta(self, nombre: str) -> Path:
        """Ruta de una nota, siempre dentro de la carpeta de memoria."""
        return self.carpeta / f"{self._nombre_limpio(nombre)}.md"

    def nombres(self) -> list[str]:
        return sorted(nota.stem for nota in self.carpeta.glob("*.md"))

    def leer(self, nombre: str) -> str:
        ruta = self.ruta(nombre)

        if not ruta.exists():
            return f"(no existe la nota «{nombre}». Notas que sí tienes: {', '.join(self.nombres())})"

        return ruta.read_text(encoding="utf-8")

    def guardar(self, nombre: str, contenido: str) -> str:
        ruta = self.ruta(nombre)
        texto = str(contenido).strip()
        ruta.write_text(texto + "\n", encoding="utf-8")
        return f"nota «{ruta.stem}» guardada ({len(texto)} caracteres)"

    def anexar(self, nombre: str, texto: str) -> str:
        ruta = self.ruta(nombre)
        añadido = str(texto).strip()

        with ruta.open("a", encoding="utf-8") as fichero:
            fichero.write(f"\n{añadido}\n")

        return f"añadido a «{ruta.stem}» ({len(añadido)} caracteres)"

    def buscar(self, texto: str, maximo: int = 12) -> str:
        """Busca un texto en todas las notas y devuelve dónde aparece."""
        aguja = str(texto).lower().strip()

        if not aguja:
            return "(búsqueda vacía)"

        encontrado = []
        for nota in self.nombres():
            for numero, linea in enumerate(self.leer(nota).splitlines(), 1):
                if aguja in linea.lower():
                    encontrado.append(f"{nota}.md:{numero}: {linea.strip()[:160]}")
                    if len(encontrado) >= maximo:
                        return "\n".join(encontrado)

        return "\n".join(encontrado) or "(no hay nada en la memoria sobre eso)"

    def indice(self) -> str:
        """Resumen de lo que hay en memoria (notas y su primera línea)."""
        notas = self.nombres()

        if not notas:
            return "(la memoria está vacía: todavía no has guardado nada)"

        lineas = []
        for nota in notas:
            contenido = [linea.strip() for linea in self.leer(nota).splitlines() if linea.strip()]
            pista = contenido[0][:140] if contenido else "(vacía)"
            lineas.append(f"- {nota}.md ({len(contenido)} líneas): {pista}")

        return "\n".join(lineas)

    def contexto(self, maximo: int = 7000) -> str:
        """La memoria entera si es pequeña, o solo el índice si se hace muy larga.

        Meter el contenido directamente evita que el modelo tenga que acordarse de
        buscar: si le preguntas por ti y no consulta sus notas, contesta mal.
        """
        notas = self.nombres()

        if not notas:
            return "(la memoria está vacía: todavía no has guardado nada)"

        if sum(len(self.leer(nota)) for nota in notas) <= maximo:
            trozos = [f"### {nota}.md\n{self.leer(nota).strip()}" for nota in notas]
            return "CONTENIDO DE TU MEMORIA:\n\n" + "\n\n".join(trozos)

        return (
            "TU MEMORIA ES GRANDE: usa leer_nota y buscar_en_memoria para consultarla.\n"
            "Índice de lo que tienes:\n" + self.indice()
        )


# Las herramientas que el modelo puede usar para tocar su memoria o aplicaciones.
HERRAMIENTAS = [
    {
        "type": "function",
        "function": {
            "name": "leer_nota",
            "description": "Lee una nota de tu memoria",
            "parameters": {
                "type": "object",
                "properties": {"nota": {"type": "string", "description": "nombre de la nota"}},
                "required": ["nota"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "guardar_nota",
            "description": "Escribe una nota: la crea o reemplaza su contenido",
            "parameters": {
                "type": "object",
                "properties": {
                    "nota": {"type": "string", "description": "nombre corto de la nota"},
                    "contenido": {"type": "string", "description": "texto completo de la nota"},
                },
                "required": ["nota", "contenido"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "anexar_nota",
            "description": "Añade texto al final de una nota sin borrar nada",
            "parameters": {
                "type": "object",
                "properties": {
                    "nota": {"type": "string", "description": "nombre de la nota"},
                    "texto": {"type": "string", "description": "texto a añadir al final"},
                },
                "required": ["nota", "texto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_en_memoria",
            "description": "Busca un texto en todas tus notas",
            "parameters": {
                "type": "object",
                "properties": {"texto": {"type": "string", "description": "qué buscar"}},
                "required": ["texto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abrir_aplicacion",
            "description": "Abre una aplicación permitida del PC, como Spotify, WhatsApp, navegador, calculadora o bloc de notas",
            "parameters": {
                "type": "object",
                "properties": {
                    "aplicacion": {"type": "string", "enum": ["spotify", "whatsapp", "navegador", "calculadora", "bloc_de_notas"]}
                },
                "required": ["aplicacion"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_en_spotify",
            "description": "Abre Spotify buscando una canción, artista, álbum o playlist; no reproduce ni compra nada automáticamente",
            "parameters": {
                "type": "object",
                "properties": {"busqueda": {"type": "string", "description": "canción, artista o playlist"}},
                "required": ["busqueda"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "preparar_whatsapp",
            "description": "Prepara un WhatsApp y pide confirmación antes de abrir el enlace de envío; nunca lo manda sin confirmación",
            "parameters": {
                "type": "object",
                "properties": {
                    "telefono": {"type": "string", "description": "número con prefijo internacional, por ejemplo +34600000000"},
                    "mensaje": {"type": "string", "description": "texto del mensaje"},
                },
                "required": ["telefono", "mensaje"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirmar_whatsapp",
            "description": "Confirma y abre el WhatsApp preparado anteriormente con el mensaje ya escrito; la persona deberá pulsar Enviar",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "registrar_escritorio",
            "description": "Guarda las ventanas visibles actuales, sus aplicaciones, posiciones, tamaños y estado",
            "parameters": {
                "type": "object",
                "properties": {"nombre": {"type": "string", "description": "nombre del escritorio guardado"}},
                "required": ["nombre"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "abrir_escritorio",
            "description": "Restaura un escritorio guardado reabriendo sus aplicaciones y colocando sus ventanas",
            "parameters": {
                "type": "object",
                "properties": {"nombre": {"type": "string", "description": "nombre del escritorio guardado"}},
                "required": ["nombre"],
            },
        },
    },
]


HERRAMIENTAS_AUTO = [
    herramienta for herramienta in HERRAMIENTAS
    if herramienta["function"]["name"] in {
        "leer_nota", "guardar_nota", "anexar_nota", "buscar_en_memoria",
    }
]


class Agente:
    """Un modelo con memoria: conversa y va escribiendo lo que aprende."""

    def __init__(
        self,
        modelo: str | None = None,
        memoria: Path | str | None = None,
        nombre: str | None = None,
        respaldo: str | None = MODELO_RESPALDO,
        usar_color: bool = True,
        memoria_automatica: bool = True,
    ):
        clave = (os.getenv("API_CHATGPT") or os.getenv("OPENAI_API_KEY") or "").strip()

        if not clave:
            raise SystemExit(
                "Falta la clave API_CHATGPT. Está en el .env del proyecto o en "
                f"{CARPETA / '.env'}:\n  API_CHATGPT=sk-..."
            )

        self.client = OpenAI(api_key=clave, base_url=BASE_URL_DEFECTO)
        self.modelo = modelo or MODELO_DEFECTO
        self.respaldo = respaldo
        self.nombre = nombre or NOMBRE_DEFECTO
        self.memoria = Memoria(memoria or MEMORIA_DEFECTO)
        self.color = usar_color
        self.memoria_automatica = memoria_automatica
        self._memoria_auto_guardada = False
        self._accion_pendiente: dict | None = None

        # Conversación de esta sesión (la memoria de verdad son las notas)
        self.historial: list[dict] = []

        # Respaldo si el modelo elegido no está disponible con esta clave
        self.candidatos_respaldo: list[str] | None = None
        self.modelos_probados: set[str] = {self.modelo}

        # Métricas de la sesión
        self.turnos = 0
        self.tokens_prompt = 0
        self.tokens_respuesta = 0
        self.tiempos_primer_token: list[float] = []
        self.tiempos_totales: list[float] = []
        self.velocidades: list[float] = []
        self.recuerdos_escritos = 0

    # --- personalidad y memoria ---

    def _prompt_sistema(self) -> str:
        """Se rehace en cada turno para que vea la memoria tal y como está ahora."""
        return (
            f"Eres {self.nombre}, agente personal. Tu idioma de trabajo es {IDIOMA_DEFECTO} "
            "(español de España). Entiendes el español hablado y respondes SIEMPRE en español, "
            "directo y sin relleno: respuestas cortas y útiles, sin repetir lo que te acaban de "
            "decir. Solo cambia de idioma si la persona te lo pide explícitamente.\n\n"
            "Puedes controlar algunas aplicaciones del PC con herramientas seguras: abrir "
            "aplicaciones, buscar música en Spotify y preparar un WhatsApp. Nunca envías un "
            "WhatsApp directamente: primero preparas el mensaje y pides confirmación; solo lo "
            "abres con el enlace prellenado después de que la persona diga claramente que sí. "
            "También puedes registrar y restaurar escritorios con nombres: registrar_escritorio "
            "guarda las ventanas visibles, aplicaciones, posiciones y tamaños; abrir_escritorio "
            "las reabre y recoloca. Si te piden registrar un escritorio, usa la herramienta "
            "directamente. Las pestañas internas de Firefox no se pueden leer con la API normal "
            "de Windows, así que informa de esa limitación y restaura como mucho la ventana y la "
            "sesión que Firefox permita recuperar.\n"
            "No ejecutes comandos de terminal ni abras rutas arbitrarias proporcionadas por el "
            "usuario.\n\n"
            "Lo que sigue es TU MEMORIA, escrita por ti en notas de texto y guardada en disco. "
            "Es lo único que recuerdas al cerrar la sesión: lo que no esté ahí, lo has olvidado.\n"
            "Úsala como memoria propia: si te preguntan por la persona, sus proyectos o lo que "
            "habéis hecho, la respuesta está abajo y no puedes decir que no lo sabes.\n\n"
            "Reglas de la memoria:\n"
            "- Guarda sin pedir permiso todo lo duradero que descubras: quién es, sus gustos, sus "
            "proyectos, sus decisiones y lo que aprendáis juntos.\n"
            "- Cuando digan «recuerda», «apunta» o «no lo olvides», TIENES que llamar a "
            "guardar_nota o anexar_nota en ese mismo turno.\n"
            "- Nunca digas que has apuntado algo si no lo has guardado de verdad: si no llamaste "
            "a la herramienta, no lo has apuntado. Es peor mentir que no guardar.\n"
            "- Añade con anexar_nota en vez de reescribir la nota entera cuando solo sumas algo.\n"
            "- Deja las notas cortas y ordenadas: son para leerlas tú y la persona.\n\n"
            f"{self.memoria.contexto()}"
        )

    def _usar_herramienta(self, nombre: str, argumentos: dict) -> str:
        """Ejecuta una herramienta de memoria y devuelve el resultado al modelo."""
        try:
            if nombre == "listar_notas":
                notas = self.memoria.nombres()
                return "Notas: " + (", ".join(notas) if notas else "(ninguna todavía)")

            if nombre == "leer_nota":
                return self.memoria.leer(argumentos.get("nota", ""))

            if nombre == "guardar_nota":
                resultado = self.memoria.guardar(argumentos.get("nota", ""), argumentos.get("contenido", ""))
                self.recuerdos_escritos += 1
                return resultado

            if nombre == "anexar_nota":
                resultado = self.memoria.anexar(argumentos.get("nota", ""), argumentos.get("texto", ""))
                self.recuerdos_escritos += 1
                return resultado

            if nombre == "buscar_en_memoria":
                return self.memoria.buscar(argumentos.get("texto", ""))

            if nombre == "abrir_aplicacion":
                return self._abrir_aplicacion(argumentos.get("aplicacion", ""))

            if nombre == "buscar_en_spotify":
                busqueda = str(argumentos.get("busqueda", "")).strip()
                if not busqueda:
                    return "(indica qué quieres buscar en Spotify)"
                webbrowser.open("https://open.spotify.com/search/" + urllib.parse.quote(busqueda))
                return f"búsqueda abierta en Spotify: {busqueda}"

            if nombre == "preparar_whatsapp":
                return self._preparar_whatsapp(argumentos.get("telefono", ""), argumentos.get("mensaje", ""))

            if nombre == "confirmar_whatsapp":
                return self._confirmar_whatsapp()

            if nombre == "registrar_escritorio":
                return _guardar_escritorio(argumentos.get("nombre", ""))

            if nombre == "abrir_escritorio":
                return _restaurar_escritorio(argumentos.get("nombre", ""))

            return f"(herramienta desconocida: {nombre})"
        except Exception as error:
            return f"(no se pudo hacer: {type(error).__name__}: {error})"

    def _abrir_aplicacion(self, aplicacion: str) -> str:
        """Abre solo destinos de una lista fija; nunca ejecuta texto como comando."""
        destinos = {
            "spotify": "spotify:",
            "whatsapp": "whatsapp:",
            "navegador": "https://www.google.com",
            "calculadora": "calc.exe",
            "bloc_de_notas": "notepad.exe",
        }
        clave = str(aplicacion).strip().lower()
        destino = destinos.get(clave)
        if not destino:
            return "(aplicación no permitida; solo puedo abrir Spotify, WhatsApp, navegador, calculadora o bloc de notas)"
        try:
            os.startfile(destino)
            return f"{clave} abierto"
        except OSError as error:
            return f"(no se pudo abrir {clave}: {error})"

    def _preparar_whatsapp(self, telefono: str, mensaje: str) -> str:
        """Guarda un WhatsApp pendiente; no abre ni envía nada todavía."""
        numero = re.sub(r"[^0-9+]", "", str(telefono)).replace("+", "", 1)
        texto = str(mensaje).strip()
        if len(numero) < 8 or not texto:
            return "(necesito un teléfono con prefijo internacional y un mensaje no vacío)"
        self._accion_pendiente = {"telefono": numero, "mensaje": texto}
        return f"CONFIRMACIÓN NECESARIA: WhatsApp para +{numero}: «{texto}». Espera que la persona diga que sí y llama después a confirmar_whatsapp."

    def _confirmar_whatsapp(self) -> str:
        """Abre WhatsApp con el mensaje preparado; el usuario aún pulsa Enviar."""
        if not self._accion_pendiente:
            return "(no hay ningún WhatsApp pendiente de confirmar)"
        pendiente = self._accion_pendiente
        enlace = "https://wa.me/{}?text={}".format(
            pendiente["telefono"], urllib.parse.quote(pendiente["mensaje"]),
        )
        webbrowser.open(enlace)
        self._accion_pendiente = None
        return "WhatsApp abierto con el destinatario y el texto preparados. La persona debe revisar y pulsar Enviar."

    # --- hablarle al modelo ---

    def _siguiente_respaldo(self) -> str | None:
        """Otro modelo que probar si el elegido no está disponible con esta clave."""
        if not self.respaldo:
            return None

        if self.candidatos_respaldo is None:
            try:
                libres = [m.id for m in self.client.models.list().data if m.id.endswith(":free")]
            except Exception:
                libres = []

            self.candidatos_respaldo = [self.respaldo] + [m for m in libres if m != self.respaldo]

        return next((m for m in self.candidatos_respaldo if m not in self.modelos_probados), None)

    def _ronda(self, mensajes: list[dict], imprimir: bool, estado: dict):
        """Una llamada al modelo en streaming. Devuelve (texto, herramientas, uso)."""
        opciones = {
            "model": self.modelo,
            "messages": mensajes,
            "tools": HERRAMIENTAS,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        try:
            flujo = self.client.chat.completions.create(**opciones)
        except Exception:
            # Algunos proxies no soportan stream_options
            opciones.pop("stream_options", None)
            flujo = self.client.chat.completions.create(**opciones)

        contenido = ""
        llamadas: dict[int, dict] = {}
        uso = None
        escrito = False

        for trozo in flujo:
            if getattr(trozo, "usage", None):
                uso = trozo.usage

            if not trozo.choices:
                continue

            delta = trozo.choices[0].delta

            if delta.content:
                if estado["ttft"] is None:
                    estado["ttft"] = time.perf_counter() - estado["inicio"]

                # El nombre solo se enseña si de verdad va a decir algo
                if imprimir and not escrito:
                    print(self.cifra(), end="", flush=True)
                    escrito = True

                contenido += delta.content
                if imprimir:
                    print(delta.content, end="", flush=True)

            # Las herramientas llegan a trozos: las vamos montando
            for parcial in (delta.tool_calls or []):
                posicion = parcial.index if parcial.index is not None else 0
                actual = llamadas.setdefault(posicion, {"id": "", "name": "", "arguments": ""})

                if parcial.id:
                    actual["id"] = parcial.id
                if parcial.function and parcial.function.name:
                    actual["name"] += parcial.function.name
                if parcial.function and parcial.function.arguments:
                    actual["arguments"] += parcial.function.arguments

        if escrito:
            print()

        return contenido, [llamadas[clave] for clave in sorted(llamadas)], uso

    def cifra(self, activo: bool = True) -> str:
        """Prefijo con el nombre del agente, como en un chat."""
        etiqueta = f"\n{self.nombre.lower()}> "
        return f"{VERDE}{etiqueta}{RESET}" if activo and self.color else etiqueta

    def preguntar(self, texto: str, imprimir: bool = True, registrar: bool = True) -> Metricas:
        """Un turno completo: contesta y usa la memoria si le hace falta."""
        self.historial.append({"role": "user", "content": texto})

        metricas = Metricas()
        escritos_antes = self.recuerdos_escritos
        estado = {"inicio": time.perf_counter(), "ttft": None}
        mensajes = [{"role": "system", "content": self._prompt_sistema()}] + self.historial
        respuesta = ""
        tokens_prompt = 0
        tokens_respuesta = 0

        for ronda in range(MAX_RONDAS):
            try:
                contenido, llamadas, uso = self._ronda(mensajes, imprimir, estado)
            except Exception as error:
                # Si el modelo no está disponible, probamos con otro
                if es_error_de_modelo(error):
                    candidato = self._siguiente_respaldo()
                    if candidato:
                        self.modelos_probados.add(candidato)
                        log(f"{ROJO}[!] {self.modelo} no está disponible -> {candidato}{RESET}")
                        self.modelo = candidato
                        return self.preguntar(texto, imprimir=imprimir, registrar=registrar)

                self.historial.pop()
                metricas.error = error_legible(error)
                metricas.total = time.perf_counter() - estado["inicio"]
                if imprimir:
                    print(f"{ROJO}Error: {metricas.error}{RESET}")
                return metricas

            if uso:
                tokens_prompt += uso.prompt_tokens
                tokens_respuesta += uso.completion_tokens

            respuesta += contenido

            if not llamadas:
                break  # ya está: respuesta final sin tocar la memoria

            # Guardamos lo que dijo y lo que hizo, para que el modelo siga el hilo
            mensajes.append({
                "role": "assistant",
                "content": contenido or None,
                "tool_calls": [
                    {"id": llamada["id"], "type": "function",
                     "function": {"name": llamada["name"], "arguments": llamada["arguments"]}}
                    for llamada in llamadas
                ],
            })

            for llamada in llamadas:
                try:
                    argumentos = json.loads(llamada["arguments"] or "{}")
                except json.JSONDecodeError:
                    argumentos = {}

                resultado = self._usar_herramienta(llamada["name"], argumentos)
                # La traza se enseña siempre: saber cuándo apunta algo es la mitad del truco
                log(f"{GRIS}  [memoria] {llamada['name']} -> {resultado[:90]}{RESET}")

                mensajes.append({"role": "tool", "tool_call_id": llamada["id"], "content": resultado})

        # Red de seguridad: si pidió recordar algo y el modelo no lo guardó, lo
        # apuntamos tal cual lo dijo él. Sin inventar nada y sin perderlo.
        if pide_recordar(texto) and self.recuerdos_escritos == escritos_antes:
            resultado = self.memoria.anexar("recuerdos-sueltos", f"- {texto}")
            self.recuerdos_escritos += 1
            log(f"{GRIS}  [memoria] anexar_nota -> {resultado} (tal cual lo dijiste){RESET}")

        metricas.texto = respuesta.strip()
        metricas.ttft = estado["ttft"]
        metricas.total = time.perf_counter() - estado["inicio"]
        metricas.tokens_prompt = tokens_prompt or None
        metricas.tokens_respuesta = tokens_respuesta or None

        # Si la API no dio el conteo, lo estimamos a ojo (4 caracteres por token)
        if not metricas.tokens_respuesta:
            metricas.tokens_respuesta = max(1, len(metricas.texto) // 4)

        self.historial.append({"role": "assistant", "content": metricas.texto})

        if registrar:
            self._acumular(metricas)

        return metricas

    def guardar_memoria_automatica(self) -> None:
        """Extrae al cerrar la sesión los datos duraderos que contó la persona.

        Es una segunda revisión breve y separada de la respuesta normal. Solo el
        modelo decide qué merece ser recordado, pero únicamente puede escribir
        mediante las herramientas de memoria; si no hay nada duradero, no toca
        ningún fichero.
        """
        if (
            not self.memoria_automatica
            or self._memoria_auto_guardada
            or not any(mensaje.get("role") == "user" for mensaje in self.historial)
        ):
            return

        self._memoria_auto_guardada = True
        conversacion = "\n".join(
            f"{mensaje['role']}: {mensaje.get('content') or ''}"
            for mensaje in self.historial
            if mensaje.get("role") in {"user", "assistant"}
        )
        conversacion = conversacion[-14000:]

        mensajes = [
            {
                "role": "system",
                "content": (
                    "Eres el archivista de un agente personal. Trabajas siempre en español. "
                    "Revisa la conversación y guarda SOLO información duradera y útil que la "
                    "persona haya dicho explícitamente: preferencias, identidad, proyectos, "
                    "decisiones, objetivos o lecciones de trabajo. No guardes saludos, preguntas "
                    "sueltas, respuestas del agente, secretos, claves API ni datos temporales.\n\n"
                    "La memoria actual está incluida en el contexto. No dupliques datos que ya "
                    "estén escritos. Usa leer_nota o buscar_en_memoria si necesitas comprobarlo; "
                    "usa anexar_nota para añadir una viñeta a una nota existente y guardar_nota "
                    "solo si necesitas crear o reemplazar una nota. Si no hay nada nuevo, no uses "
                    "ninguna herramienta. No expliques el proceso: esta es una tarea interna.\n\n"
                    f"{self.memoria.contexto()}"
                ),
            },
            {
                "role": "user",
                "content": (
                    "Consolida lo importante de esta conversación en tu memoria. "
                    "La conversación es:\n\n" + conversacion
                ),
            },
        ]

        try:
            for _ in range(4):
                respuesta = self.client.chat.completions.create(
                    model=self.modelo,
                    messages=mensajes,
                    tools=HERRAMIENTAS_AUTO,
                )
                mensaje = respuesta.choices[0].message
                llamadas = mensaje.tool_calls or []
                if not llamadas:
                    return

                mensajes.append({
                    "role": "assistant",
                    "content": mensaje.content or None,
                    "tool_calls": [
                        {
                            "id": llamada.id,
                            "type": "function",
                            "function": {
                                "name": llamada.function.name,
                                "arguments": llamada.function.arguments,
                            },
                        }
                        for llamada in llamadas
                    ],
                })

                for llamada in llamadas:
                    try:
                        argumentos = json.loads(llamada.function.arguments or "{}")
                    except json.JSONDecodeError:
                        argumentos = {}
                    resultado = self._usar_herramienta(llamada.function.name, argumentos)
                    log(
                        f"{GRIS}  [memoria automática] {llamada.function.name} -> "
                        f"{resultado[:90]}{RESET}"
                    )
                    mensajes.append({
                        "role": "tool",
                        "tool_call_id": llamada.id,
                        "content": resultado,
                    })
        except Exception as error:
            # Guardar memoria es opcional: nunca debe impedir cerrar el chat.
            log(f"{GRIS}[i] No se pudo actualizar la memoria automática: {error}{RESET}")

    def cerrar_sesion(self) -> None:
        """Persiste los recuerdos nuevos antes de terminar el proceso."""
        self.guardar_memoria_automatica()

    def nueva_conversacion(self) -> None:
        """Guarda lo aprendido y empieza otra conversación sin borrar las notas."""
        self.guardar_memoria_automatica()
        self.historial.clear()
        self._memoria_auto_guardada = False

    def _acumular(self, metricas: Metricas) -> None:
        self.turnos += 1
        self.tokens_prompt += metricas.tokens_prompt or 0
        self.tokens_respuesta += metricas.tokens_respuesta or 0
        self.tiempos_totales.append(metricas.total)

        if metricas.ttft is not None:
            self.tiempos_primer_token.append(metricas.ttft)

        velocidad = metricas.tokens_por_segundo
        if velocidad:
            self.velocidades.append(velocidad)

    # --- información de la sesión ---

    def resumen(self) -> None:
        """Lo que ha pasado en la sesión y lo que hay en la memoria."""
        def promedio(valores: list[float]) -> float | None:
            return sum(valores) / len(valores) if valores else None

        ttft = promedio(self.tiempos_primer_token)
        total = promedio(self.tiempos_totales)
        velocidad = promedio(self.velocidades)
        notas = self.memoria.nombres()

        print(f"{AZUL}\n── Sesión ────────────────────────────────{RESET}")
        print(f"  Agente:            {self.nombre}")
        print(f"  Modelo:            {self.modelo}")
        print(f"  Turnos:            {self.turnos}")
        print(f"  1er token (medio): {f'{ttft * 1000:.0f} ms' if ttft else 'n/d'}")
        print(f"  Latencia media:    {f'{total:.2f} s' if total else 'n/d'}")
        print(f"  Velocidad media:   {f'{velocidad:.1f} tok/s' if velocidad else 'n/d'}")
        print(f"  Tokens:            {self.tokens_prompt} + {self.tokens_respuesta}")
        print(f"{AZUL}── Memoria ({len(notas)} notas) ──────────────{RESET}")
        print(f"  Carpeta:           {self.memoria.carpeta}")
        print(f"  Recuerdos escritos: {self.recuerdos_escritos} en esta sesión")

        for nota in notas:
            print(f"   - {nota}.md")
