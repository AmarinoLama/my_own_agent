"""Modo voz con una mini-barra notch que refleja la actividad del agente.

La barra negra queda fija y centrada en el borde superior de la pantalla:
esquinas inferiores redondeadas y codos invertidos que la sueldan al borde.

En medio se dibujan 5 puntos de frecuencia, sin texto: crecen y encogen con el
sonido. Azules y en línea cuando hablas tú, girando en círculo cuando piensa y
naranjas y en línea simulando la voz cuando te contesta. En pausa se ven grises
con el símbolo ⏸ debajo. Haz clic para pausar o reanudar y clic derecho para
abrir el menú.

    python voz.py                       # hablar por micrófono
    python voz.py --texto               # escribir en la consola (sin micro)
    python voz.py --acceso-directo      # crea el icono en el escritorio
    python voz.py --modelo deepseek-v4.1-flash:free

Escuchar usa Silero VAD para no confundir tu voz con el ruido de la habitación
(y WebRTC o la energía si no hay librerías) y transcribe en español con Google
o con whisper local; hablar usa la voz neural de Edge TTS (edge-tts + pygame)
con reserva de la voz de Windows. Con `pip install faster-whisper` la
transcripción puede ser 100 % local.
"""

import argparse
import collections
import ctypes
import json
import math
import queue
import re
import signal
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import unicodedata
import os
import urllib.error
import urllib.request
import warnings as _warnings
from pathlib import Path

try:
    # audioop es C puro: RMS y pico sin recorrer las muestras en Python.
    # (Python 3.13 ya no lo trae, por si acaso hay un respaldo abajo.)
    with _warnings.catch_warnings():
        _warnings.simplefilter("ignore", DeprecationWarning)
        import audioop
except ImportError:
    audioop = None

try:
    from PIL import Image, ImageDraw
except ImportError:
    Image = ImageDraw = None

from chat_cli import MODELO_DEFECTO, MODELO_RESPALDO, ChatCLI, configurar_salida

RAIZ = Path(__file__).resolve().parent
CONFIG = RAIZ / "voz_config.json"

# Única instancia de PyAudio del programa (ver _pyaudio)
_AUDIO = None

# Colores ANSI para la consola (el orbe tiene los suyos)
AZUL, VERDE, GRIS, ROJO, RESET = "\033[36m", "\033[32m", "\033[90m", "\033[31m", "\033[0m"

# Intentamos cargar las librerías de Windows para la voz
try:
    import pythoncom
    import win32com.client

    SAPI_DISPONIBLE = True
except ImportError:
    SAPI_DISPONIBLE = False

# Banderas de SAPI
SVSF_ASYNC = 1                 # hablar sin bloquear el hilo
SVSF_PURGE_BEFORE_SPEAK = 2     # cortar lo que se está diciendo
SRE_RECOGNITION = 16            # nos avisan cuando entiende una frase
SRE_HYPOTHESIS = 32             # y cuando va entendiendo a medias
SRE_FALSE_RECOGNITION = 512
SRE_SOUND_START = 2             # empieza a oír algo
SRE_SOUND_END = 4               # deja de oír algo
SGDS_ACTIVE = 1                 # dictado activo


def log(*partes) -> None:
    """print seguro: si se lanza con pythonw (sin consola) escribe en voz.log."""
    texto = " ".join(str(p) for p in partes)
    if sys.stdout is None:
        try:
            with open(RAIZ / "voz.log", "a", encoding="utf-8") as fichero:
                fichero.write(texto + "\n")
        except OSError:
            pass
        return
    try:
        # flush: si no, al haber una ventana por medio la consola se queda a medias
        print(texto, flush=True)
    except UnicodeEncodeError:
        # Consolas Windows en cp1252: una flecha o un símbolo raro no puede
        # escribirse y NO debemos perder el mensaje entero (log silencioso)
        codificacion = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(texto.encode(codificacion, errors="replace").decode(codificacion),
              flush=True)
    except Exception:
        pass


def limpiar_para_voz(texto: str) -> str:
    """Quita markdown, código y emojis para que la voz suene natural."""
    texto = re.sub(r"```.*?```", " ", texto, flags=re.S)            # bloques de código
    texto = re.sub(r"`([^`]*)`", r"\1", texto)                       # código en línea
    texto = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", texto)              # imágenes
    texto = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", texto)           # enlaces
    texto = re.sub(r"^[ \t]*[-*+]\s+", "", texto, flags=re.M)        # viñetas
    texto = re.sub(r"^[ \t]*#{1,6}\s*", "", texto, flags=re.M)       # títulos
    texto = re.sub(r"[*_>#|~]", "", texto)                           # resto de markdown
    texto = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF]", "", texto)  # emojis
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip()


NOMBRES_AGENTE = {"jarvis", "agente", "orbe"}

# La frase solo cierra si no habla de otra cosa: «cierra la puerta jarvis»
# sobran «la» y «puerta», así que esa no apaga nada.
VERBOS_CIERRE = {"cierra", "cierrate", "apagate", "sal", "vete", "descansa",
                 "termina", "finaliza", "adios"}
MULETILLAS_CIERRE = {"por", "favor", "ya", "ahora", "gracias", "hasta", "luego",
                     "te", "puedes", "quiero", "okay", "vale"}


def es_orden_de_cierre(texto: str) -> bool:
    """True si la frase es una orden de apagarse («ciérrate, Jarvis»).

    Exige el nombre del agente y solo palabras de cierre: sin eso, cualquier
    «sal» suelto o cualquier conversación sobre cerrar cosas lo apagaría.
    """
    limpio = unicodedata.normalize("NFD", str(texto).lower())
    limpio = "".join(letra for letra in limpio if unicodedata.category(letra) != "Mn")
    palabras = set(re.findall(r"[a-z]+", limpio))
    if not palabras:
        return False
    if not palabras & NOMBRES_AGENTE:
        return False
    if not palabras & VERBOS_CIERRE and not {"hasta", "luego"} <= palabras:
        return False
    return palabras <= (NOMBRES_AGENTE | VERBOS_CIERRE | MULETILLAS_CIERRE)


def _pyaudio():
    """Una sola instancia de PyAudio para todo el programa.

    PortAudio se lleva fatal con varias instancias creadas y destruidas en
    hilos distintos: si se hace, el programa revienta (segmentation fault).
    """
    global _AUDIO

    if _AUDIO is None:
        import pyaudio

        _AUDIO = pyaudio.PyAudio()

    return _AUDIO


def listar_microfonos() -> list[tuple[int, str, bool]]:
    """Micrófonos que se pueden usar: (índice, nombre, es_el_predeterminado)."""
    try:
        audio = _pyaudio()
    except ImportError:
        return []

    try:
        try:
            predeterminado = audio.get_default_input_device_info()["index"]
        except Exception:
            predeterminado = None

        microfonos = []
        for indice in range(audio.get_device_count()):
            try:
                info = audio.get_device_info_by_index(indice)
            except Exception:
                continue

            if info.get("maxInputChannels", 0) < 1:
                continue  # es una salida (altavoces), no una entrada

            nombre = _arreglar_nombre(str(info.get("name", f"dispositivo {indice}")))

            # El mismo micro aparece varias veces (WASAPI, DirectSound, MME...):
            # decimos por cuál entra para que se puedan distinguir
            try:
                anfitrion = audio.get_host_api_info_by_index(info["hostApi"])["name"]
                nombre += f"  [{anfitrion}]".replace("Windows ", "")
            except Exception:
                pass

            microfonos.append((indice, nombre, indice == predeterminado))

        # Agrupamos por nombre para que las copias del mismo micro queden juntas
        return sorted(microfonos, key=lambda micro: (micro[1].lower(), micro[0]))
    except Exception as error:
        log(f"{ROJO}No se pudieron listar los micrófonos: {error}{RESET}")
        return []


def _arreglar_nombre(nombre: str) -> str:
    """PyAudio devuelve los nombres con la codificación cambiada (MicrÃ³fono)."""
    try:
        return nombre.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return nombre


def nombre_micro(indice: int | None) -> str:
    """Nombre legible del micrófono que se está usando."""
    if indice is None:
        return "micrófono predeterminado de Windows"

    for numero, nombre, _ in listar_microfonos():
        if numero == indice:
            return nombre

    return f"dispositivo {indice}"


def resolver_micro(valor) -> int | None:
    """Convierte --micro (número o trozo de nombre) en un índice de dispositivo."""
    if valor is None or valor == "":
        return None

    microfonos = listar_microfonos()

    if isinstance(valor, int) or str(valor).isdigit():
        indice = int(valor)
        if any(numero == indice for numero, _, _ in microfonos):
            return indice
        log(f"{ROJO}No existe el micrófono {indice}. Disponibles: {_lista_basica(microfonos)}{RESET}")
        return None

    buscado = str(valor).lower()
    coincidencias = [(numero, nombre) for numero, nombre, _ in microfonos if buscado in nombre.lower()]

    if not coincidencias:
        log(f"{ROJO}Ningún micrófono contiene «{valor}». Disponibles: {_lista_basica(microfonos)}{RESET}")
        return None

    if len(coincidencias) == 1:
        return coincidencias[0][0]

    # El mismo micro aparece con varios sistemas de audio y no todos captan:
    # preferimos los que suelen funcionar mejor y avisamos de los demás
    for preferido in ("[MME]", "[WASAPI]", "[DirectSound]"):
        for numero, nombre in coincidencias:
            if preferido in nombre:
                otras = ", ".join(f"{n}={nom}" for n, nom in coincidencias if n != numero)
                log(f"{GRIS}«{valor}» coincide con {len(coincidencias)} entradas; uso {numero} "
                    f"({nombre}). Otras: {otras}{RESET}")
                return numero

    return coincidencias[0][0]


def _lista_basica(microfonos) -> str:
    return ", ".join(f"{numero}={nombre}" for numero, nombre, _ in microfonos) or "ninguno"


def leer_config() -> dict:
    """Ajustes guardados (por ejemplo, qué micrófono usaste la última vez)."""
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def guardar_config(**nuevo) -> None:
    """Guarda los ajustes sin perder los que ya había."""
    datos = leer_config()
    datos.update(nuevo)
    try:
        CONFIG.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


class Voz:
    """Voz de salida: usa las voces de Windows (SAPI)."""

    def __init__(self, preferida: str = "Helena", velocidad: int = 0, volumen: int = 100):
        pythoncom.CoInitialize()
        self.sapi = win32com.client.Dispatch("SAPI.SpVoice")
        self.sapi.Rate = max(-10, min(10, velocidad))
        self.sapi.Volume = max(0, min(100, volumen))
        self.nombre = self._elegir(preferida)

    def _elegir(self, preferida: str) -> str:
        """Busca la voz pedida (por nombre o idioma); si no, usa la primera."""
        voces = self.sapi.GetVoices()

        if voces.Count:
            for i in range(voces.Count):
                voz = voces.Item(i)
                if preferida and preferida.lower() in voz.GetDescription().lower():
                    self.sapi.Voice = voz
                    return voz.GetDescription()

            # mejor una voz en español que la primera inglesa que haya
            for i in range(voces.Count):
                voz = voces.Item(i)
                descripcion = voz.GetDescription().lower()
                if "spanish" in descripcion or "espa" in descripcion:
                    self.sapi.Voice = voz
                    return voz.GetDescription()

            self.sapi.Voice = voces.Item(0)
            return voces.Item(0).GetDescription()

        return "sin voces instaladas"

    def decir(self, texto: str) -> None:
        """Empieza a hablar sin bloquear."""
        if texto:
            self.sapi.Speak(texto, SVSF_ASYNC)

    def hablando(self) -> bool:
        """True mientras siga hablando."""
        return self.sapi.Status.RunningState == 2

    def callar(self) -> None:
        """Corta la frase actual (para poder interrumpirle)."""
        self.sapi.Speak("", SVSF_ASYNC | SVSF_PURGE_BEFORE_SPEAK)

    def esperar(self, comprobar_cada: float = 0.08, al_paso=None) -> None:
        """Espera a terminar de hablar dando oportunidades de interrumpir."""
        # Speak es asíncrono: tarda ~50 ms en ponerse en marcha (RunningState
        # pasa de 0 a 2). Sin esta espera, esperar() saldría al instante y la
        # app empezaría a escuchar mientras la voz aún suena.
        inicio = time.time()
        while not self.hablando() and time.time() - inicio < 0.3:
            time.sleep(comprobar_cada)
        while self.hablando():
            if al_paso and al_paso():
                return
            time.sleep(comprobar_cada)


class VozFishAudio:
    """Síntesis de voz de Fish Audio usando una voz clonada o compartida.

    Fish devuelve audio WAV desde su API. Se reproduce directamente con el
    reproductor de Windows, sin guardar conversaciones ni la clave en disco.
    Requiere FISH_AUDIO_API_KEY y FISH_AUDIO_VOICE_ID en .env.
    """

    def __init__(self, voz_id: str | None = None, velocidad: int = 0, volumen: int = 100):
        try:
            import winsound
        except ImportError as error:
            raise RuntimeError("Fish Audio necesita Windows para reproducir el audio") from error

        self.winsound = winsound
        self.api_key = (os.getenv("FISH_AUDIO_API_KEY") or os.getenv("FISH_API_KEY") or "").strip()
        self.voz_id = (voz_id or os.getenv("FISH_AUDIO_VOICE_ID") or "").strip()
        self.endpoint = os.getenv("FISH_AUDIO_ENDPOINT", "https://api.fish.audio/v1/tts").strip()
        self.volumen = max(0, min(100, volumen))
        self._termina = 0.0
        self._bloqueo = threading.Lock()

        if not self.api_key:
            raise RuntimeError("Falta FISH_AUDIO_API_KEY en .env")
        if not self.voz_id:
            raise RuntimeError("Falta FISH_AUDIO_VOICE_ID en .env")

        self.nombre = f"Fish Audio · {self.voz_id}"

    def decir(self, texto: str) -> None:
        """Genera y reproduce una respuesta en español de forma asíncrona."""
        if not texto:
            return

        datos = json.dumps({
            "text": texto,
            "reference_id": self.voz_id,
            "format": "wav",
            "latency": "normal",
        }).encode("utf-8")
        peticion = urllib.request.Request(
            self.endpoint,
            data=datos,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "audio/wav",
            },
        )

        try:
            with urllib.request.urlopen(peticion, timeout=60) as respuesta:
                audio = respuesta.read()
        except urllib.error.HTTPError as error:
            detalle = error.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"Fish Audio HTTP {error.code}: {detalle}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Fish Audio no responde: {error.reason}") from error

        if not audio or audio[:4] != b"RIFF":
            raise RuntimeError("Fish Audio no devolvió un WAV válido")

        # Duración aproximada del WAV para que el orbe sepa cuándo ha terminado.
        duracion = 0.0
        try:
            import wave
            import io
            with wave.open(io.BytesIO(audio), "rb") as wav:
                duracion = wav.getnframes() / float(wav.getframerate() or 1)
        except Exception:
            duracion = max(0.5, len(audio) / 32000)

        with self._bloqueo:
            self._termina = time.time() + duracion
        self.winsound.PlaySound(audio, self.winsound.SND_MEMORY | self.winsound.SND_ASYNC)

    def hablando(self) -> bool:
        with self._bloqueo:
            return time.time() < self._termina

    def callar(self) -> None:
        self.winsound.PlaySound(None, self.winsound.SND_PURGE)
        with self._bloqueo:
            self._termina = 0.0

    def esperar(self, comprobar_cada: float = 0.08, al_paso=None) -> None:
        while self.hablando():
            if al_paso and al_paso():
                return
            time.sleep(comprobar_cada)


class VozEdge:
    """Voz neural de Microsoft Edge (edge-tts): suena mucho mejor que SAPI.

    Descarga un MP3 con edge-tts (necesita red) y lo suena con pygame, con la
    misma interfaz que el resto de voces (decir/hablando/callar/esperar).
    EDGE_TTS_VOICE elige la voz (por defecto es-ES-ElviraNeural); si la red
    falla o no hay tarjeta de sonido, contesta con la voz de Windows (Helena,
    español) para no quedarse muda.
    """

    def __init__(self, voz_id: str | None = None, velocidad: int = 0, volumen: int = 100):
        try:
            import edge_tts
            import pygame
        except ImportError as error:
            raise RuntimeError("faltan librerías: pip install edge-tts pygame") from error

        self._edge = edge_tts
        self._pygame = pygame
        self.voz_id = (voz_id or os.getenv("EDGE_TTS_VOICE") or "es-ES-ElviraNeural").strip()
        # La velocidad llega como la de SAPI (-10 a 10); edge pide un porcentaje
        self._velocidad = max(-10, min(10, velocidad))
        self._rate = f"{self._velocidad * 10:+d}%"
        self._volumen = max(0, min(100, volumen)) / 100
        self._volumen_sapi = max(0, min(100, volumen))
        try:
            pygame.mixer.init()
        except Exception as error:
            raise RuntimeError(f"sin tarjeta de sonido: {error}") from error

        self._bloqueo = threading.Lock()
        self._generacion = 0
        self._hablando = False
        self._sonando = False
        self._inicio_decir = 0.0
        self._traslado = 0.0
        self._respaldo: Voz | None = None
        self.nombre = f"Edge TTS · {self.voz_id}"

    def decir(self, texto: str) -> None:
        """Genera el audio con edge-tts y lo suena sin bloquear."""
        if not texto:
            return
        with self._bloqueo:
            self._generacion += 1
            generacion = self._generacion
            self._hablando = True   # cuenta como "hablando" también al generar
            self._sonando = False
            self._inicio_decir = time.time()
        threading.Thread(target=self._hilar, args=(texto, generacion),
                         daemon=True).start()

    def _sintetizar(self, texto: str) -> bytes:
        import asyncio

        async def descargar() -> bytes:
            comunicado = self._edge.Communicate(texto, self.voz_id, rate=self._rate)
            audio = bytearray()

            async def con_tope():
                async for trozo in comunicado.stream():
                    if trozo.get("type") == "audio":
                        audio.extend(trozo["data"])

            await asyncio.wait_for(con_tope(), timeout=20)
            return bytes(audio)

        return asyncio.run(descargar())

    def _hilar(self, texto: str, generacion: int) -> None:
        import tempfile

        ruta = None
        delegado = False
        try:
            mp3 = self._sintetizar(texto)
            if not mp3:
                raise RuntimeError("no devolvió audio")
            with self._bloqueo:
                if generacion != self._generacion:
                    return   # le han callado o ya empieza otra frase
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fichero:
                fichero.write(mp3)
                ruta = fichero.name
            self._pygame.mixer.music.load(ruta)
            self._pygame.mixer.music.set_volume(self._volumen)
            self._pygame.mixer.music.play()
            with self._bloqueo:
                if generacion == self._generacion:
                    self._sonando = True
            while self._pygame.mixer.music.get_busy():
                with self._bloqueo:
                    if generacion != self._generacion:
                        break
                time.sleep(0.05)
        except Exception as error:
            log(f"{ROJO}[voz] Edge TTS: {error}{RESET} - pruebo con la voz de Windows")
            delegado = self._respaldar(texto, generacion)
        finally:
            with self._bloqueo:
                vigente = generacion == self._generacion
                if vigente and not delegado:
                    self._hablando = False
                    self._sonando = False
            if ruta:
                if vigente:
                    try:
                        self._pygame.mixer.music.unload()
                    except Exception:
                        pass
                try:
                    os.unlink(ruta)
                except OSError:
                    pass

    def _respaldar(self, texto: str, generacion: int) -> bool:
        """Si Edge falla, que hable la voz de Windows para no quedarse muda."""
        with self._bloqueo:
            if generacion != self._generacion:
                return False
            if self._respaldo is None:
                try:
                    self._respaldo = Voz("Helena", self._velocidad, self._volumen_sapi)
                except Exception:
                    self._hablando = False
                    return False
            respaldo = self._respaldo
            self._hablando = False   # a partir de aquí manda SAPI
            self._sonando = True     # y sigue "hablando" mientras suena
            self._traslado = time.time()
        try:
            respaldo.decir(texto)
        except Exception as error:
            log(f"{ROJO}[voz] sin audio: {error}{RESET}")
            with self._bloqueo:
                self._hablando = False
                self._sonando = False
            return False
        return True

    def hablando(self) -> bool:
        with self._bloqueo:
            if self._hablando:
                return True
            if self._sonando:   # respaldo SAPI en marcha
                # SAPI tarda ~50 ms en arrancar: no soltamos la escucha antes
                if time.time() - self._traslado < 0.3:
                    return True
                respaldo = self._respaldo
                if respaldo is not None and respaldo.hablando():
                    return True
                self._sonando = False
                return False
            return False

    def callar(self) -> None:
        with self._bloqueo:
            self._generacion += 1   # el hilo vigente se tira fuera
            self._hablando = False
            self._sonando = False
            respaldo = self._respaldo
        try:
            self._pygame.mixer.music.stop()
        except Exception:
            pass
        if respaldo is not None:
            respaldo.callar()

    def esperar(self, comprobar_cada: float = 0.08, al_paso=None) -> None:
        """Espera a terminar de hablar; corta si la red no da audio en 20 s."""
        while self.hablando():
            with self._bloqueo:
                arrancado = self._sonando
                desde = self._inicio_decir
            if not arrancado and time.time() - desde > 20.0:
                log(f"{ROJO}[voz] Edge TTS no responde; lo corto{RESET}")
                self.callar()
                return
            if al_paso and al_paso():
                return
            time.sleep(comprobar_cada)


class _EventosSAPI:
    """Recibe los avisos del reconocedor de Windows y los guarda en su oído."""

    oido = None

    def OnRecognition(self, *args):
        try:
            texto = args[-1].PhraseInfo.GetText().strip()
        except Exception:
            return
        if _EventosSAPI.oido and len(texto) > 1:
            _EventosSAPI.oido.frases.put(texto)
            _EventosSAPI.oido.parcial = ""

    def OnHypothesis(self, *args):
        try:
            texto = args[-1].PhraseInfo.GetText().strip()
        except Exception:
            return
        if _EventosSAPI.oido:
            _EventosSAPI.oido.parcial = texto

    def OnSoundStart(self, *args):
        if _EventosSAPI.oido:
            _EventosSAPI.oido.hablando_ahora = True

    def OnSoundEnd(self, *args):
        if _EventosSAPI.oido:
            _EventosSAPI.oido.hablando_ahora = False  # ha dejado de oírse algo


class OidoSAPI:
    """Oído offline con el reconocedor de voz de Windows (en español)."""

    def __init__(self):
        self.frases: queue.Queue[str] = queue.Queue()
        self.parcial = ""
        self.hablando_ahora = False
        self.nivel = 0.0
        self.descripcion = "Windows"

        pythoncom.CoInitialize()
        self.reconocedor = win32com.client.Dispatch("SAPI.SpInprocRecognizer")
        self.reconocedor.AudioInputStream = win32com.client.Dispatch("SAPI.SpMMAudioIn")

        if self.reconocedor.Recognizer:
            self.descripcion = self.reconocedor.Recognizer.GetDescription()

        self.contexto = self.reconocedor.CreateRecoContext()
        self.contexto.EventInterests = (
            SRE_RECOGNITION | SRE_HYPOTHESIS | SRE_FALSE_RECOGNITION
            | SRE_SOUND_START | SRE_SOUND_END
        )

        self.gramatica = self.contexto.CreateGrammar()
        self.gramatica.DictationSetState(SGDS_ACTIVE)

        # Conectamos los eventos (necesitan este objeto antes de crearse)
        _EventosSAPI.oido = self
        self.eventos = win32com.client.WithEvents(self.contexto, _EventosSAPI)

    def _bombear(self) -> None:
        """Deja que Windows entregue los eventos pendientes."""
        pythoncom.PumpWaitingMessages()

    def escuchar(self, parar: threading.Event, espera_max: float | None = None,
                 al_progreso=None) -> str | None:
        """Espera hasta que entienda una frase completa y la devuelve."""
        limite = time.time() + espera_max if espera_max else None

        while not parar.is_set():
            self._bombear()
            self.nivel = 1.0 if self.hablando_ahora else 0.0

            try:
                return self.frases.get_nowait()
            except queue.Empty:
                pass

            # Este motor va entendiendo a medias mientras hablas: lo enseñamos
            if al_progreso:
                al_progreso("oido", self.parcial, self.nivel)

            if limite and time.time() > limite:
                return None

            time.sleep(0.02)

        return None

    def frase_inmediata(self) -> str | None:
        """Mira si ya hay una frase sin esperar (para interrumpirle)."""
        self._bombear()
        try:
            return self.frases.get_nowait()
        except queue.Empty:
            return None

    def descartar(self) -> None:
        """Tira lo que se haya oído (al pausar)."""
        self.parcial = ""
        self.nivel = 0.0
        while True:
            try:
                self.frases.get_nowait()
            except queue.Empty:
                return

    def cambiar_micro(self, indice: int | None) -> None:
        """El reconocedor de Windows usa el micrófono que tenga Windows por defecto."""
        log(f"{GRIS}Este motor usa el micrófono predeterminado de Windows; "
            f"para elegirlo usa --oido google{RESET}")

    def abortar(self) -> None:
        return None


class _VadSilero:
    """VAD de Silero (red ligera, torch) con histéresis y AGC propio.

    Recetas de habitaciones ruidosas: el suelo de ruido medido queda en
    prob 0.001–0.03, así que se entra con 0.45 y se sale con 0.3 (la voz débil
    pasa y el ruido no). Además un AGC acerca los picos de voz a ~7000 RMS
    antes de clasificar, porque un micro Bluetooth o alejado llega muy bajo y
    la red decide mejor con nivel. Pide ventanas de 512 muestras a 16 kHz.
    """

    VENTANA = 512  # muestras @ 16 kHz
    ENTRADA = 0.45
    SALIDA = 0.3
    PICO_OBJETIVO = 7000.0
    GANANCIA_MAX = 30.0

    def __init__(self):
        import warnings

        from silero_vad import load_silero_vad

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._modelo = load_silero_vad()
        self._torch = __import__("torch")
        self._pendientes: list[int] = []
        self._en_voz = False
        self._ultimo = 0.0
        self._pico = 1000.0  # pico RMS reciente; manda la ganancia del AGC

    def is_speech(self, marco: bytes, frecuencia: int) -> bool:
        if frecuencia != 16000:
            marco = DetectorVoz.a_16k(marco, frecuencia)  # el micro puede ir a 44,1 kHz
        if len(marco) % 2:
            marco = marco[:-1]
        muestras = struct.unpack(f"<{len(marco) // 2}h", marco)
        if not muestras:
            return self._en_voz

        rms = (sum(m * m for m in muestras) / len(muestras)) ** 0.5
        self._pico = max(rms, self._pico * 0.995)
        ganancia = min(self.GANANCIA_MAX,
                       max(1.0, self.PICO_OBJETIVO / max(self._pico, 300.0)))
        if ganancia > 1.0:
            muestras = tuple(int(max(-32767, min(32767, m * ganancia)))
                             for m in muestras)

        self._pendientes.extend(muestras)
        while len(self._pendientes) >= self.VENTANA:
            ventana = self._pendientes[: self.VENTANA]
            del self._pendientes[: self.VENTANA]
            tensor = self._torch.tensor(ventana, dtype=self._torch.float32) / 32768.0
            with self._torch.no_grad():
                self._ultimo = float(self._modelo(tensor, 16000))
            self._en_voz = self._ultimo >= (self.ENTRADA if not self._en_voz
                                            else self.SALIDA)
        return self._en_voz


class _VadWebRTC:
    """WebRTC VAD en modo 3 (el más exigente): barato y sin dependencias pesadas."""

    def __init__(self):
        import webrtcvad

        self._vad = webrtcvad.Vad(3)

    def is_speech(self, marco: bytes, frecuencia: int) -> bool:
        if frecuencia != 16000:
            marco = DetectorVoz.a_16k(marco, frecuencia)
        return self._vad.is_speech(marco, 16000)


def _cargar_vad():
    """Silero si está (el mejor con ruido), si no WebRTC, si no nada."""
    try:
        return _VadSilero()
    except Exception:
        pass
    try:
        return _VadWebRTC()
    except Exception:
        return None


class DetectorVoz:
    """¿Hay voz en este marco? VAD de verdad (Silero/WebRTC) con reserva por energía.

    Port de lo que usan los asistentes open source (RealtimeSTT, wyoming…):
    el VAD decide, una ventana de confirmación descarta ráfagas aisladas y el
    umbral de energía queda como reserva si no hay librería de VAD, así un
    ventilador no abre frases ni las corta.
    """

    FRECUENCIA = 16000
    BYTES_MARCO = int(FRECUENCIA * 0.02) * 2  # 20 ms @ 16 kHz, sin firmar
    UMBRAL_RUIDO = 400.0      # RMS mínimo sin VAD (reserva)
    VENTANA_VOZ = 6            # marcos que mira voz_sostenida (20 ms c/u)
    MINIMO_VOZ = 3             # al menos 3 de los últimos 6 marcos con voz
    SUELO_MARCOS = 250         # ~5 s de historial para el suelo de ruido

    def __init__(self, frecuencia: int = FRECUENCIA, umbral: float | None = None):
        self.frecuencia = frecuencia
        self._vad = _cargar_vad()
        self.historial: collections.deque = collections.deque(maxlen=self.VENTANA_VOZ)
        self.umbral_energia = umbral or self.UMBRAL_RUIDO
        self._suelo: collections.deque = collections.deque(maxlen=self.SUELO_MARCOS)
        self._cuenta_suelo = 0

    @staticmethod
    def muestras_marco(frecuencia: int) -> int:
        """20 ms de audio (lo que piden WebRTC y Silero)."""
        return int(frecuencia * 0.02)

    @classmethod
    def bytes_marco(cls, frecuencia: int) -> int:
        return cls.muestras_marco(frecuencia) * 2

    @staticmethod
    def a_16k(datos: bytes, frecuencia: int) -> bytes:
        """Remuestrea a 16 kHz con interpolación lineal (lo que pide Silero)."""
        if frecuencia == 16000:
            return datos
        muestras = struct.unpack(f"<{len(datos) // 2}h", datos)
        salida = int(len(muestras) * 16000 / frecuencia)
        if salida <= 0:
            return b""
        escalados = []
        for i in range(salida):
            posicion = i * frecuencia / 16000
            primero = min(int(posicion), len(muestras) - 1)
            segundo = min(primero + 1, len(muestras) - 1)
            peso = posicion - primero
            valor = muestras[primero] * (1 - peso) + muestras[segundo] * peso
            escalados.append(int(max(-32768, min(32767, round(valor)))))
        return struct.pack(f"<{salida}h", *escalados)

    @staticmethod
    def voz_sostenida(historial) -> bool:
        """True si la voz lleva varios marcos: descarta el ruido aislado."""
        estados = [estado for _, estado in historial]
        ventana = estados[-DetectorVoz.VENTANA_VOZ:]
        return sum(ventana) >= DetectorVoz.MINIMO_VOZ

    @staticmethod
    def _rms(datos: bytes) -> float:
        """RMS del marco (en C con audioop; en Python si no está)."""
        if not datos:
            return 0.0
        if audioop is not None:
            return float(audioop.rms(datos, 2))
        muestras = struct.unpack(f"<{len(datos) // 2}h", datos)
        if not muestras:
            return 0.0
        return (sum(m * m for m in muestras) / len(muestras)) ** 0.5

    def es_voz(self, marco: bytes) -> bool:
        """True si el marco suena a voz (VAD primero; energía como reserva)."""
        rms = self._rms(marco)
        umbral = getattr(self, "umbral_energia", self.UMBRAL_RUIDO)
        # Por debajo del ruido de la habitación no hay nada que clasificar: el
        # VAD (una red neuronal) es lo caro de este bucle, y en reposo la sala
        # está todo el rato por debajo de este umbral. El suelo se adapta solo,
        # así que la voz —que siempre va muy por encima— sigue pasando.
        if rms < umbral * 0.6:
            return False
        if self._vad is not None:
            try:
                return bool(self._vad.is_speech(marco, getattr(self, "frecuencia", 16000)))
            except Exception:
                pass  # frecuencia no soportada: caemos a la energía
        return rms >= umbral

    def registrar(self, marco: bytes, hay_voz: bool) -> None:
        """Anota el marco: historial para voz_sostenida y suelo de ruido adaptativo."""
        self.historial.append((marco, hay_voz))
        rms = self._rms(marco)
        if not rms and not marco:
            return
        self._suelo.append(rms)
        self._cuenta_suelo += 1
        if self._cuenta_suelo >= 25 and len(self._suelo) >= 25:
            # El suelo es el ruido de fondo (percentil bajo); el umbral es 2,5×
            # ese suelo, como en RealtimeSTT. Sirve de reserva sin VAD y de
            # puerta de entrada al VAD (es_voz): por eso se adapta siempre.
            ordenado = sorted(self._suelo)
            fondo = ordenado[int(len(ordenado) * 0.2)]
            self.umbral_energia = max(150.0, fondo * 2.5 + 25.0)
            self._cuenta_suelo = 0


class FraseVoz:
    """Junta los marcos de voz en una frase y la cierra cuando callas (endpointing).

    Recetas de los proyectos de voz en español: preroll para no cortar la
    primera sílaba, voz mínima para no abrir con un ruido aislado, silencio de
    cierre configurable (0.5–0.7 s) y tope de duración para no quedarse atrapado
    si el ruido no para nunca.
    """

    def __init__(self, frecuencia: int, silencio: float = 0.6, preroll: float = 0.3,
                 voz_minima: float = 0.1, duracion_maxima: float = 20.0):
        self.frecuencia = frecuencia
        self.silencio = silencio
        self.preroll = preroll
        self.voz_minima = voz_minima
        self.duracion_maxima = duracion_maxima
        self.escuchando = False
        self._previos: collections.deque = collections.deque()
        self._previos_segundos = 0.0
        self._frase = bytearray()
        self._voz_segundos = 0.0
        self._silencio_desde = 0.0

    @staticmethod
    def _segundos(marco: bytes, frecuencia: int) -> float:
        return len(marco) / 2 / frecuencia

    def reiniciar(self) -> None:
        """Tira lo que hubiera y empieza otra vez (también tras descartar)."""
        self.escuchando = False
        self._previos.clear()
        self._previos_segundos = 0.0
        self._frase = bytearray()
        self._voz_segundos = 0.0
        self._silencio_desde = 0.0

    def _guardar_previo(self, marco: bytes) -> None:
        self._previos.append(marco)
        self._previos_segundos += self._segundos(marco, self.frecuencia)
        while len(self._previos) > 1:
            cabeza = self._segundos(self._previos[0], self.frecuencia)
            if self._previos_segundos - cabeza < self.preroll:
                break
            sale = self._previos.popleft()
            self._previos_segundos -= self._segundos(sale, self.frecuencia)

    def alimentar(self, marco: bytes, hay_voz: bool) -> bytes | None:
        """Mete un marco; devuelve la frase entera cuando has terminado de hablar."""
        dur = self._segundos(marco, self.frecuencia)

        if not self.escuchando:
            self._guardar_previo(marco)
            if hay_voz:
                self._voz_segundos += dur
                if self._voz_segundos >= self.voz_minima:
                    # Empieza la frase: lo del preroll va delante para no
                    # perder la primera sílaba.
                    self.escuchando = True
                    self._frase = bytearray(b"".join(self._previos))
                    self._previos.clear()
                    self._previos_segundos = 0.0
                    self._silencio_desde = 0.0
            else:
                self._voz_segundos = max(0.0, self._voz_segundos - dur)
            return None

        self._frase += marco
        if hay_voz:
            self._silencio_desde = 0.0
        else:
            self._silencio_desde += dur

        duracion = len(self._frase) / 2 / self.frecuencia
        if duracion >= self.duracion_maxima or self._silencio_desde >= self.silencio:
            frase = bytes(self._frase)
            self.reiniciar()
            return frase
        return None


_MODELOS_WHISPER: dict = {}


def transcribir_whisper(datos: bytes, frecuencia: int = 16000,
                        idioma: str = "es-ES", nombre: str = "small") -> str | None:
    """Transcripción local con faster-whisper (si está instalado)."""
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except ImportError:
        return None

    modelo = _MODELOS_WHISPER.get(nombre)
    if modelo is None:
        modelo = WhisperModel(nombre, device="cpu", compute_type="int8")
        _MODELOS_WHISPER[nombre] = modelo

    audio = np.frombuffer(datos, dtype=np.int16).astype(np.float32) / 32768.0
    if frecuencia != 16000:
        muestras = int(len(audio) * 16000 / frecuencia)
        origen = np.arange(len(audio)) * 16000 / frecuencia
        audio = np.interp(np.arange(muestras), origen, audio).astype(np.float32)

    segmentos, _ = modelo.transcribe(
        audio,
        language=idioma.split("-")[0],
        beam_size=5,
        initial_prompt="Transcripción en español de España, con puntuación natural.",
    )
    texto = " ".join(palabra.text.strip() for palabra in segmentos)
    return texto.strip() or None


class Palmadas:
    """Dos palmadas seguidas: la señal para abrir el agente aunque esté cerrado.

    Una palmada es un golpe seco: sube de golpe y en menos de 100 ms ya se ha
    deshecho, mientras que la voz se sostiene durante toda la sílaba. Se mide
    el pico de cada trozo de 20 ms; cuando uno pasa del umbral se abre un
    «golpe» que se cierra cuando el sonido baja de la cuarta parte de su propio
    pico (la reverberación de la habitación no lo alarga) y solo cuenta si ha
    durado 100 ms o menos. Dos con entre 0,22 y 1,25 s de distancia abren la
    ventana.

    Solo se miden picos en local: no transcribe, no guarda ni manda audio.
    """

    PICO_MINIMO = 8000.0    # por debajo es ruido de la habitación (clics, teclado)
    FACTOR_FONDO = 5.0      # y, en habitación ruidosa, cinco veces el pico
    DURACION_MAXIMA = 0.10   # más de 100 ms es una sílaba, no una palmada
    CIERRE_FLOJO = 3         # trozos flojos seguidos que cierran el golpe
    UMBRAL_FLOJO = 0.25      # «flojo» = por debajo de la cuarta parte
    INTERVALO_MIN = 0.22     # más pegadas: un solo golpe partido en dos
    INTERVALO_MAX = 1.25     # más separadas: otra vez desde cero
    REFRACCION = 0.10        # tras un golpe, no se cuenta nada un rato

    def __init__(self, umbral: float | None = None) -> None:
        # Umbral fijo (AGENTE_PALMADA_UMBRAL en el .env); sin él se calibra.
        self.pico_minimo = float(umbral) if umbral else self.PICO_MINIMO
        self.fondo = 400.0                 # pico habitual del ambiente
        self.en_golpe = False              # hay un sonido fuerte en marcha
        self.inicio = 0.0
        self.ultimo_fuerte = 0.0
        self.picos: list[float] = []
        self.flojos = 0
        self.silencio = 0                  # trozos flojos seguidos
        self.puede_abrir = True            # hace falta que el sonido pare antes
        self.primera: float | None = None  # momento de la primera palmada
        self._primera_golpe: dict | None = None
        self.reposo_hasta = 0.0
        self.historial: list[dict] = []    # golpes válidos de esta escucha
        self.deteccion: dict | None = None # el par que ha abierto (picos y paso)

    def calibrar(self, pico_fondo: float) -> None:
        """Fija el ruido de la habitación medido al arrancar."""
        self.fondo = max(80.0, float(pico_fondo))

    @staticmethod
    def _pico(datos: bytes) -> float:
        """Pico (valor absoluto máximo) de un trozo de audio de 16 bits."""
        if not datos:
            return 0.0
        if audioop is not None:
            return float(audioop.max(datos, 2))
        muestras = struct.unpack(f"<{len(datos) // 2}h", datos)
        return float(max((abs(m) for m in muestras), default=0))

    def reiniciar(self) -> None:
        """Se olvida de lo que lleva oído (por si hay que empezar de cero)."""
        self.en_golpe = False
        self.primera = None
        self._primera_golpe = None
        self.puede_abrir = True
        self.silencio = 0
        self.reposo_hasta = 0.0
        self.deteccion = None

    def alimentar(self, datos: bytes, ahora: float | None = None) -> bool:
        """Mete un trozo de audio; True cuando ya son dos palmadas."""
        ahora = time.time() if ahora is None else ahora
        pico = self._pico(datos)
        umbral = max(self.pico_minimo, self.fondo * self.FACTOR_FONDO)
        # El fondo sube con todo lo que suena (despacio): en una habitación
        # ruidosa hará falta una palmada más fuerte para que cuente.
        self.fondo = max(80.0, self.fondo * 0.98 + pico * 0.02)

        if not self.en_golpe:
            if pico >= umbral and self.puede_abrir:
                self.en_golpe = True
                self.inicio = ahora
                self.ultimo_fuerte = ahora
                self.picos = [pico]
                self.flojos = 0
                self.silencio = 0
            elif pico < umbral * self.UMBRAL_FLOJO:
                self.silencio += 1
                if self.silencio >= self.CIERRE_FLOJO:
                    self.puede_abrir = True   # ya hubo silencio: todo vale
            else:
                self.silencio = 0
            return False

        self.picos.append(pico)
        # El «flojo» se mide contra el pico de este mismo golpe, no contra el umbral
        # global: una palmada real se mete por debajo de la cuarta parte de su
        # pico en unos 40-80 ms, mientras que la voz se mantiene alta todo el
        # rato. Medido contra el umbral, la cola de la palmada (400-1000 en
        # reverberación) seguía «sonando» y el golpe duraba más de 100 ms, con
        # lo que se clasificaba como voz y no contaba.
        corte = max(self.picos) * self.UMBRAL_FLOJO
        if pico >= corte:
            self.ultimo_fuerte = ahora
            self.flojos = 0
        else:
            self.flojos += 1

        duracion = self.ultimo_fuerte - self.inicio
        if self.flojos < self.CIERRE_FLOJO and duracion <= self.DURACION_MAXIMA + 0.1:
            return False   # sigue sonando: aún no sabemos qué es

        self.en_golpe = False
        if self.flojos >= self.CIERRE_FLOJO:
            self.puede_abrir = True
            self.silencio = self.CIERRE_FLOJO
        else:
            # El sonido no para: que pare de verdad antes de mirar otro
            self.puede_abrir = False
            self.silencio = 0
        if duracion > self.DURACION_MAXIMA:
            return False   # se sostuvo: es voz (o la tele), no una palmada
        if ahora < self.reposo_hasta:
            return False
        self.reposo_hasta = ahora + self.REFRACCION

        golpe = {"cuando": ahora, "pico": round(max(self.picos)),
                 "duracion": round(duracion, 3)}
        self.historial.append(golpe)

        if self.primera is None:
            self.primera = ahora
            self._primera_golpe = golpe
            return False

        distancia = ahora - self.primera
        self.primera = None
        if self.INTERVALO_MIN <= distancia <= self.INTERVALO_MAX:
            self.deteccion = {
                "pico1": self._primera_golpe["pico"] if self._primera_golpe else None,
                "pico2": golpe["pico"],
                "distancia": round(distancia, 2),
            }
            return True
        self.primera = ahora   # ni muy pegada ni muy separada: cuenta como primera
        self._primera_golpe = golpe
        return False


class OidoGoogle:
    """Oído con Silero VAD + Google (o whisper local), con detección propia de voz.

    Google no devuelve texto mientras hablas, así que leemos el micrófono
    nosotros mismos por trocitos: medimos el nivel (para que el orbe lo refleje),
    el VAD decide si hay voz (no el ruido de la habitación), y cuando llevas un
    rato hablando y callas, mandamos el audio a transcribir. Así responde en
    cuanto te callas, sin esperas raras.
    """

    CONFIANZA_MINIMA = 0.25  # por debajo, es ruido: no se transcribe

    def __init__(self, idioma: str = "es-ES", silencio_final: float = 0.7,
                 micro: int | None = None, transcripcion: str = "auto",
                 modelo_whisper: str = "small"):
        import speech_recognition as sr

        self.sr = sr
        self.idioma = idioma
        self.silencio_final = silencio_final  # segundos de silencio = has acabado
        self.indice_micro = micro             # None = el predeterminado de Windows
        self.motor = transcripcion            # "auto" | "whisper" | "google"
        self.modelo_whisper = modelo_whisper
        self.perfil_voz = None
        self.pedido_cambio: int | None = None
        self.abortar_escucha = False
        self.parcial = ""
        self.nivel = 0.0
        self.umbral_maximo = 6000.0
        self.detector: DetectorVoz | None = None
        self.segmentador: FraseVoz | None = None

        # Escucha de interrupción mientras el agente está hablando (barge-in).
        # Se mantiene un único stream abierto durante esa fase y se cierra al
        # terminar, para no competir con la escucha normal del mismo micrófono.
        self._interrupcion_fuente = None
        self._interrupcion_detector: DetectorVoz | None = None
        self._interrupcion_segmentador: FraseVoz | None = None
        self._interrupcion_ultimo_error = None

        self.reconocedor = sr.Recognizer()
        self._abrir_micro()

    FRECUENCIA_MIC = 16000     # donde viven el VAD y Whisper: sin remuestrear

    def _abrir_micro(self) -> None:
        """Abre el micrófono elegido y mide el ruido de fondo para el umbral."""
        # A 16 kHz el audio llega ya como lo quieren el VAD y Whisper: el
        # remuestreo de 44,1 kHz (0,3 ms por marco, ~2 % de CPU) lo hace el
        # motor de audio de Windows y no este bucle. Si el micro no acepta
        # esa frecuencia, se queda con la suya de toda la vida.
        self.micro = None
        ultimo_error: Exception | None = None
        for frecuencia in (self.FRECUENCIA_MIC, None):
            try:
                micro = (self.sr.Microphone(device_index=self.indice_micro,
                                            sample_rate=frecuencia)
                         if frecuencia is not None
                         else self.sr.Microphone(device_index=self.indice_micro))
                with micro as fuente:
                    if fuente.stream is None:
                        raise RuntimeError(f"no admite {frecuencia} Hz")
                    self.reconocedor.adjust_for_ambient_noise(fuente, duration=0.6)
                self.micro = micro
                break
            except Exception as error:
                ultimo_error = error

        if self.micro is None:
            log(f"{ROJO}No se pudo abrir el micrófono {self.indice_micro} "
                f"({ultimo_error}); uso el predeterminado{RESET}")
            self.indice_micro = None
            self.micro = self.sr.Microphone()
            with self.micro as fuente:
                self.reconocedor.adjust_for_ambient_noise(fuente, duration=0.6)

        etiqueta = {"google": "Google", "whisper": f"Whisper {self.modelo_whisper}"}
        motor = etiqueta.get(self.motor, "Google/Whisper")
        self.descripcion = f"{motor} ({self.idioma}) · {nombre_micro(self.indice_micro)}"

        # Trocitos de 20 ms: pyaudio cuenta muestras (frames), no bytes
        self.tamano_bloque = DetectorVoz.muestras_marco(self.micro.SAMPLE_RATE)

        # El umbral sale del ruido de fondo medido al arrancar (solo para el
        # nivel del orbe y de reserva; la voz la decide el VAD)
        self.umbral_minimo = max(150.0, self.reconocedor.energy_threshold * 1.15)
        self.umbral = self.umbral_minimo
        self.escala = self.umbral * 5   # nivel 1.0 cuando hablas clarito
        self.detector = DetectorVoz(self.micro.SAMPLE_RATE, umbral=self.umbral)
        self.segmentador = None
        vad = type(self.detector._vad).__name__ if self.detector._vad else "energia"
        log(f"{GRIS}Detección de voz: {vad} · micro a "
            f"{self.micro.SAMPLE_RATE} Hz{RESET}")

    def cambiar_micro(self, indice: int | None) -> None:
        """Pide cambiar de micrófono: se aplica en cuanto suelte el que tiene abierto."""
        self.pedido_cambio = indice
        self.abortar_escucha = True

    def abortar(self) -> None:
        """Suelta el micrófono cuanto antes (para poder abrirlo desde el selector)."""
        self.abortar_escucha = True

    def _ajustar_umbral(self, factor: float) -> None:
        """Sube o baja el umbral para adaptarse al ruido de la habitación."""
        self.umbral = min(self.umbral_maximo, max(self.umbral_minimo, self.umbral * factor))
        self.escala = self.umbral * 5

    @staticmethod
    def _energia(datos: bytes) -> float:
        """Volumen medio del trocito de audio (RMS)."""
        if not datos:
            return 0.0
        if audioop is not None:
            return float(audioop.rms(datos, 2))
        muestras = struct.unpack(f"<{len(datos) // 2}h", datos)
        if not muestras:
            return 0.0
        return (sum(m * m for m in muestras) / len(muestras)) ** 0.5

    def escuchar(self, parar: threading.Event, espera_max: float | None = None,
                 al_progreso=None) -> str | None:
        """Espera a que hables, te escucha y transcribe en cuanto te callas."""
        # Si han pedido otro micrófono, lo abrimos ahora (aquí ya está libre)
        if self.pedido_cambio is not None or self.abortar_escucha:
            if self.pedido_cambio is not None:
                self.indice_micro = self.pedido_cambio
                self.pedido_cambio = None
                self._abrir_micro()
                log(f"{GRIS}Micrófono: {self.descripcion}{RESET}")
            self.abortar_escucha = False
            return None

        inicio = time.time()
        frecuencia = self.micro.SAMPLE_RATE
        self.segmentador = FraseVoz(
            frecuencia,
            silencio=self.silencio_final,
            preroll=0.3,
            voz_minima=0.1,          # 100 ms de voz: un ruido no abre frase
            duracion_maxima=20.0,    # tope por si el ruido no para nunca
        )
        frase = None
        sin_voz_segundos = 0.0
        aviso_vad = 0.0

        with self.micro as fuente:
            while not parar.is_set() and frase is None:
                # Cambio de micrófono o pausa: salimos y soltamos el dispositivo
                if self.abortar_escucha:
                    self.abortar_escucha = False
                    self.segmentador.reiniciar()
                    return None

                datos = fuente.stream.read(self.tamano_bloque)
                if not datos:
                    continue

                # Nivel del orbe (energía) y voz de verdad (VAD)
                energia = self._energia(datos)
                self.nivel = min(1.0, energia / self.escala) if energia else 0.0
                hay_voz = self.detector.es_voz(datos)
                self.detector.registrar(datos, hay_voz)
                # Confirmación: hace falta voz sostenida, no una ráfaga
                voz = DetectorVoz.voz_sostenida(self.detector.historial)

                # Diagnóstico: hay volumen pero el VAD no llega a confirmar voz
                # (micro lejos, muy ruidoso o ganancia baja): se avisa una vez
                # cada 10 s para que se pueda ajustar sin adivinar.
                if not self.segmentador.escuchando and not voz and self.nivel > 0.35:
                    sin_voz_segundos += len(datos) / 2 / self.micro.SAMPLE_RATE
                    if (sin_voz_segundos >= 2.5 and time.time() - aviso_vad > 10.0
                            and getattr(self.detector, "_vad", None) is not None):
                        prob = getattr(self.detector._vad, "_ultimo", -1.0)
                        log(f"{GRIS}[vad] hay volumen ({self.nivel:.2f}) pero la voz "
                            f"no se confirma (prob {prob:.2f}); acércate o baja ruido{RESET}")
                        aviso_vad = time.time()
                        sin_voz_segundos = 0.0
                else:
                    sin_voz_segundos = 0.0

                frase = self.segmentador.alimentar(datos, voz)

                if al_progreso:
                    al_progreso("oido", "", self.nivel)

                if (frase is None and not self.segmentador.escuchando
                        and espera_max and time.time() - inicio > espera_max):
                    return None  # no habla: no le cortamos una frase a medias

        self.nivel = 0.0
        self.segmentador = None
        if frase is None:
            return None

        if al_progreso:
            al_progreso("transcribiendo", "", 0.0)

        return self._transcribir_mensaje(frase)

    def _transcribir_mensaje(self, datos: bytes) -> str | None:
        """Transcribe la frase: whisper local si se pidió, Google con confianza."""
        duracion = len(datos) / 2 / self.micro.SAMPLE_RATE

        if self.motor in ("whisper", "auto"):
            texto = transcribir_whisper(datos, self.micro.SAMPLE_RATE,
                                        self.idioma, self.modelo_whisper)
            if texto:
                log(f"{GRIS}[frase] {duracion:.1f} s - whisper: «{texto[:80]}»{RESET}")
                return texto
            if self.motor == "whisper":
                log(f"{GRIS}[i] Sin faster-whisper (pip install faster-whisper); "
                    f"uso Google{RESET}")

        trozo = self.sr.AudioData(datos, self.micro.SAMPLE_RATE, self.micro.SAMPLE_WIDTH)
        try:
            respuesta = self.reconocedor.recognize_google(
                trozo, language=self.idioma, show_all=True)
        except self.sr.UnknownValueError:
            self._ajustar_umbral(1.1)  # no lo ha entendido: probablemente era ruido
            log(f"{GRIS}[frase] {duracion:.1f} s - Google no lo entiende (ruido){RESET}")
            return None
        except self.sr.RequestError as error:
            raise RuntimeError(f"Google no responde: {error}") from error

        texto = self._mejor_alternativa(respuesta)
        if texto is None:
            self._ajustar_umbral(1.1)
            log(f"{GRIS}[frase] {duracion:.1f} s - sin transcripción "
                f"(confianza baja o ruido){RESET}")
        else:
            log(f"{GRIS}[frase] {duracion:.1f} s - «{texto[:80]}»{RESET}")
        return texto

    @classmethod
    def _mejor_alternativa(cls, respuesta) -> str | None:
        """De la respuesta de Google se queda con la alternativa más fiable.

        Si la confianza es bajísima (menos de CONFIANZA_MINIMA) es que era
        ruido: mejor no transcribir nada que escribir una payasada.
        """
        if not isinstance(respuesta, dict):
            texto = str(respuesta or "").strip()
            return texto or None

        alternativas = respuesta.get("alternative")
        if alternativas is None:
            resultados = respuesta.get("result") or []
            alternativas = (resultados[0].get("alternative") if resultados else None) or []

        mejor_texto, mejor_confianza = None, None
        for alternativa in alternativas:
            texto = (alternativa.get("transcript") or "").strip()
            if not texto:
                continue
            confianza = alternativa.get("confidence")
            if mejor_texto is None or (confianza is not None and (mejor_confianza is None
                                                                  or confianza > mejor_confianza)):
                mejor_texto, mejor_confianza = texto, confianza

        if mejor_texto is None:
            return None
        if mejor_confianza is None:
            return mejor_texto  # Google a veces no da confianza: en la duda, sí
        if mejor_confianza < cls.CONFIANZA_MINIMA:
            return None
        return mejor_texto

    def _transcribir_interrupcion(self, datos: bytes) -> str | None:
        """Transcribe lo que interrumpiste; exige confianza para no alucinar."""
        try:
            audio = self.sr.AudioData(datos, self.micro.SAMPLE_RATE, self.micro.SAMPLE_WIDTH)
            respuesta = self.reconocedor.recognize_google(
                audio, language=self.idioma, show_all=True)
        except self.sr.UnknownValueError:
            return None
        except self.sr.RequestError as error:
            log(f"{GRIS}No se pudo transcribir la interrupción: {error}{RESET}")
            return None
        return self._mejor_alternativa(respuesta)

    def _cerrar_interrupcion(self) -> None:
        """Cierra el stream temporal usado para detectar que nos interrumpes."""
        if self._interrupcion_fuente is not None:
            try:
                self.micro.__exit__(None, None, None)
            except Exception:
                pass
        self._interrupcion_fuente = None
        self._interrupcion_detector = None
        self._interrupcion_segmentador = None

    def frase_inmediata(self) -> str | None:
        """Detecta una frase nueva mientras el agente está hablando.

        SpeechRecognition no ofrece texto parcial de Google en tiempo real, pero
        sí podemos leer trozos cortos del mismo micrófono mientras se reproduce
        la respuesta: el VAD separa tu voz del ruido y, cuando llevas hablando
        un rato y callas, mandamos ese audio a transcribir para interrumpir.
        """
        if self._interrupcion_fuente is None:
            try:
                # __enter__ deja el stream abierto entre llamadas a este método.
                self._interrupcion_fuente = self.micro.__enter__()
                self._interrupcion_detector = DetectorVoz(self.micro.SAMPLE_RATE,
                                                           umbral=self.umbral)
                self._interrupcion_segmentador = FraseVoz(
                    self.micro.SAMPLE_RATE,
                    silencio=0.4,     # más rápido que la escucha normal
                    preroll=0.2,
                    voz_minima=0.1,
                    duracion_maxima=10.0,
                )
            except Exception as error:
                self._interrupcion_ultimo_error = str(error)
                self._cerrar_interrupcion()
                return None

        try:
            fuente = self._interrupcion_fuente
            datos = fuente.stream.read(self.tamano_bloque)
            if not datos:
                return None

            hay_voz = self._interrupcion_detector.es_voz(datos)
            self._interrupcion_detector.registrar(datos, hay_voz)
            voz = DetectorVoz.voz_sostenida(self._interrupcion_detector.historial)

            frase = self._interrupcion_segmentador.alimentar(datos, voz)
            if frase is None:
                return None

            self._cerrar_interrupcion()
            return self._transcribir_interrupcion(frase)
        except Exception as error:
            self._interrupcion_ultimo_error = str(error)
            self._cerrar_interrupcion()
            return None

    def descartar(self) -> None:
        self.nivel = 0.0
        self.parcial = ""
        if self.segmentador is not None:
            self.segmentador.reiniciar()
        self._cerrar_interrupcion()

    def cambiar_micro(self, indice: int | None) -> None:
        self._cerrar_interrupcion()
        log(f"{GRIS}Este oído no permite cambiar de micrófono mientras habla; usa --oído google{RESET}")

    def abortar(self) -> None:
        self._cerrar_interrupcion()
        return None


class OidoTeclado:
    """Sin micrófono: escribes en la consola y el orbe sigue funcionando."""

    descripcion = "teclado"
    parcial = ""
    nivel = 0.0

    def escuchar(self, parar: threading.Event, espera_max: float | None = None,
                 al_progreso=None) -> str | None:
        try:
            linea = input("tú> ")
        except EOFError as error:
            raise RuntimeError("no hay consola para escribir (usa el micrófono)") from error

        return linea.strip() or None

    def frase_inmediata(self) -> str | None:
        return None

    def descartar(self) -> None:
        return None

    def cambiar_micro(self, indice: int | None) -> None:
        return None

    def abortar(self) -> None:
        return None


def crear_oido(preferido: str = "auto", silencio: float = 0.7,
               micro: int | None = None, idioma: str = "es-ES",
               transcripcion: str = "auto", modelo_whisper: str = "small"):
    """Elige el oído disponible (por defecto, reconocimiento en español)."""
    if preferido in ("auto", "google"):
        try:
            return OidoGoogle(idioma=idioma, silencio_final=silencio, micro=micro,
                              transcripcion=transcripcion,
                              modelo_whisper=modelo_whisper)
        except Exception as error:
            if preferido == "google":
                raise
            log(f"{GRIS}[i] Sin SpeechRecognition ({type(error).__name__}), uso el de Windows{RESET}")

    if not SAPI_DISPONIBLE:
        raise RuntimeError("Falta pywin32: pip install pywin32")

    return OidoSAPI()


class Orbe:
    """Barra-notch con los 5 puntos de frecuencia (el estado del agente).

    Los 5 puntos hacen de vúmetro: crecen y encogen con el sonido. En línea y
    azules cuando hablas tú; girando en círculo cuando piensa; de vuelta en
    línea, naranjas y moviéndose al ritmo de la voz cuando te contesta.
    """

    ANCHO, ALTO = 280, 64
    ALTO_PASTILLA = 58          # la barra negra ocupa casi toda la ventana
    INSET = 16                  # margen lateral de la pastilla
    RADIO_INFERIOR = 22         # esquinas redondeadas de abajo
    RADIO_INVERSO = 14          # codos cóncavos que la sueldan al borde
    CLAVE = "#0f1115"  # color clave: lo que queda fuera de la pastilla se ve a través
    CLAVE_RGB = (15, 17, 21)
    NEGRO = (0, 0, 0)           # color de la pastilla

    # Caja donde se pintan los 5 puntos, en px dentro de la pastilla
    ANCHO_PUNTOS, ALTO_PUNTOS = 176, 56
    PUNTOS = 5
    SEPARACION = 14.0       # de un punto a otro cuando están alineados
    ORBITA = 13.0           # radio del círculo que dan al pensar
    RADIO_BASE = 3.0        # radio en reposo
    RADIO_MAX = 7.5         # radio con el sonido al tope
    GIRO = 3.0              # rad/s del círculo mientras piensa
    ESCALA_RENDER = 4       # se pinta x4 y se reduce: bordes suaves
    # (factor del halo, brillo) del círculo más grande al núcleo del punto
    HALO = ((1.9, 0.10), (1.5, 0.20), (1.25, 0.38), (1.1, 0.62), (1.0, 1.0))

    # Cosas que salen iguales en todos los fotogramas: se guardan en cuanto se
    # dibujan una vez, en vez de recalcularlas 25 veces por segundo.
    _mascara_cache: dict = {}
    _base_cache: dict = {}

    # Color de los puntos por estado: azul = tu voz, naranja = la suya
    COLOR = {
        "inactivo": (174, 196, 224),
        "pausado": (98, 106, 118),
        "escuchando": (88, 166, 255),
        "pensando": (170, 134, 255),
        "hablando": (255, 158, 64),
        "error": (255, 84, 84),
    }

    def __init__(self, al_click, al_salir, titulo: str = "Voz", al_elegir_micro=None):
        self.al_click = al_click
        self.al_salir = al_salir
        self.al_elegir_micro = al_elegir_micro
        self.cola: queue.Queue[tuple] = queue.Queue()

        self.root = tk.Tk()
        self.root.title(titulo)
        self.root.overrideredirect(True)                     # sin bordes ni barra
        self.root.attributes("-topmost", True)               # siempre por encima
        try:
            self.root.attributes("-transparentcolor", self.CLAVE)
        except tk.TclError:
            pass
        self.root.attributes("-alpha", 0.95)
        self.root.configure(bg=self.CLAVE)
        # Con pythonw no hay consola: si un clic de Tk revienta, que quede en voz.log
        self.root.report_callback_exception = self._error_tk

        # Pegada al borde superior y centrada: como un notch de móvil.
        x = (self.root.winfo_screenwidth() - self.ANCHO) // 2
        self.root.geometry(f"{self.ANCHO}x{self.ALTO}+{x}+0")

        self.lienzo = tk.Canvas(
            self.root, width=self.ANCHO, height=self.ALTO,
            bg=self.CLAVE, highlightthickness=0,
        )
        self.lienzo.pack()

        self.cx = self.ANCHO // 2
        self.sprite = self.lienzo.create_image(self.cx, self.ALTO // 2)

        self.estado_actual = "inactivo"
        self.tiempo = 0.0
        self.pulso = 0.0
        self.nivel_objetivo = 0.0   # lo que oye ahora mismo el micrófono
        self.nivel_visible = 0.0    # suavizado, para que no dé tirones
        self._ultimo_tick = time.time()
        self._foto = None

        # La barra está fija: clic para pausar, clic derecho para el menú
        self.lienzo.bind("<Button-1>", lambda _evento: self.al_click())
        self.lienzo.bind("<Button-3>", self._menu)

        self._menu_emergente = tk.Menu(self.root, tearoff=0)
        self._menu_emergente.add_command(label="Pausar / reanudar", command=self.al_click)
        self._menu_emergente.add_command(
            label="Elegir micrófono…",
            command=lambda: self.al_elegir_micro and self.al_elegir_micro(),
        )
        self._menu_emergente.add_separator()
        self._menu_emergente.add_command(label="Salir", command=self.al_salir)
        self._pintar()

        self.root.after(40, self._tick)

    # --- cosas que llaman otros hilos (van por cola, Tk no es thread-safe) ---

    def estado(self, nombre: str) -> None:
        self.cola.put(("estado", nombre))

    def progreso(self, nivel: float) -> None:
        """Lo que está oyendo en este instante: solo el nivel de voz (vúmetro)."""
        self.cola.put(("progreso", nivel))

    def pedir_cierre(self) -> None:
        """Pide salir desde otro hilo (la orden «ciérrate, Jarvis»).

        Tk solo se toca desde el suyo, así que el cierre viaja por la cola y
        lo hace `_tick` en la próxima animación.
        """
        self.cola.put(("salir", None))

    # --- interacción ---

    def _error_tk(self, tipo, valor, traza) -> None:
        """Fallo en un manejador de Tk: sin consola se perdería, va a voz.log."""
        log(f"{ROJO}[tk] {tipo.__name__}: {valor}{RESET}")
        log(traza)

    def _menu(self, evento) -> None:
        try:
            self._menu_emergente.tk_popup(evento.x_root, evento.y_root)
        finally:
            self._menu_emergente.grab_release()

    # --- dibujo ---

    def _orbita(self) -> float:
        """0 = los puntos alineados; 1 = girando en círculo (cuando piensa)."""
        objetivo = 1.0 if self.estado_actual == "pensando" else 0.0
        actual = getattr(self, "orbita", None)
        if actual is None:
            actual = objetivo
        # entra y sale del círculo despacio: al hablar «se deshacen» y se alinean
        self.orbita = actual + (objetivo - actual) * 0.12
        return self.orbita

    def _amplitud(self, i: int) -> float:
        """Tamaño del punto i (0 a 1): el sonido que representa en su estado."""
        t = self.tiempo
        estado = self.estado_actual
        if estado == "escuchando":
            # Tu voz: el nivel del micro recorriendo los 5 puntos, como un espectro.
            # La voz normal llega al micro con nivel 0.2-0.4, así que se amplifica
            # para que el salto se vea de verdad y no un temblor apenas perceptible.
            nivel = min(1.0, getattr(self, "nivel_visible", 0.0) * 2.2)
            onda = 0.5 + 0.5 * math.sin(t * 8.0 + i * 1.35)
            amplitud = nivel * (0.35 + 0.65 * onda)
        elif estado == "hablando":
            # Su voz: sílabas rápidas moduladas por el ritmo de la frase
            silaba = 0.5 + 0.5 * math.sin(t * 19.0)
            frase = 0.55 + 0.45 * math.sin(t * 3.1 + 1.2)
            onda = 0.4 + 0.6 * (0.5 + 0.5 * math.sin(t * 13.0 + i * 1.1))
            amplitud = silaba * frase * onda
        elif estado == "pensando":
            amplitud = 0.30 + 0.20 * math.sin(t * 4.0 + i * 1.2)
        elif estado == "error":
            amplitud = 0.35 + 0.65 * abs(math.sin(t * 21.0 + i * 0.7))
        elif estado == "pausado":
            amplitud = 0.10 + 0.05 * math.sin(t * 1.2 + i)
        else:  # inactivo: respiración lenta
            amplitud = 0.22 + 0.14 * math.sin(t * 2.0 + i * 0.6)
        return max(0.0, min(1.0, amplitud))

    def _puntos(self):
        """Los 5 puntos: (x, y, radio, tinta) en px dentro de la pastilla."""
        orbita = self._orbita()
        color = self.COLOR.get(self.estado_actual, self.COLOR["inactivo"])
        sacudida = (1.6 * math.sin(self.tiempo * 23.0)
                    if self.estado_actual == "error" else 0.0)
        puntos = []
        for i in range(self.PUNTOS):
            x_linea = (i - (self.PUNTOS - 1) / 2) * self.SEPARACION + sacudida
            angulo = math.tau * i / self.PUNTOS + self.tiempo * self.GIRO
            x_circulo = math.cos(angulo) * self.ORBITA + sacudida
            y_circulo = math.sin(angulo) * self.ORBITA
            amplitud = self._amplitud(i)
            radio = self.RADIO_BASE + (self.RADIO_MAX - self.RADIO_BASE) * amplitud
            brillo = 0.55 + 0.45 * amplitud
            puntos.append((
                x_linea + (x_circulo - x_linea) * orbita,
                y_circulo * orbita,
                radio,
                tuple(int(c * brillo) for c in color),
            ))
        return puntos

    @classmethod
    def _pintar_puntos(cls, dibujo, puntos, centro_x, centro_y, escala) -> None:
        """Dibuja los 5 puntos con su halo, de fuera hacia dentro.

        Se pintan todos los halos del mismo tamaño antes que los núcleos: si no,
        el halo de un punto se comería el núcleo del vecino y saldría a media luna.
        """
        for factor, brillo in cls.HALO:
            for x, y, radio, tinta in puntos:
                r = radio * factor * escala
                color = tuple(int(c * brillo) for c in tinta)
                cx = centro_x + x * escala
                cy = centro_y + y * escala
                dibujo.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)

    @classmethod
    def _pintar_pausa(cls, dibujo, cx, cy, escala) -> None:
        """El símbolo ⏸ bajo los puntos: se ve de un vistazo que está en pausa."""
        tinta = cls.COLOR["pausado"]
        ancho, alto = 3.4 * escala, 11 * escala
        hueco = 5.0 * escala
        y0 = cy + 14 * escala
        for lado in (-1, 1):
            x = cx + lado * hueco / 2
            dibujo.rounded_rectangle(
                (round(x - ancho / 2), round(y0),
                 round(x + ancho / 2), round(y0 + alto)),
                radius=max(1, round(ancho / 2)), fill=tinta,
            )

    @classmethod
    def _mascara_pastilla(cls, escala: int = 4):
        """Máscara (modo 'L') de la pastilla: redondeada abajo y con los codos
        invertidos arriba, dibujada a `escala`× y reducida para suavizar.

        No cambia nunca: se calcula una vez y se guarda (antes se redibujaba
        y reescalaba en cada fotograma, que era de lo más caro del orbe).
        """
        cache = cls._mascara_cache
        if escala in cache:
            return cache[escala]
        e = escala
        ancho, alto = cls.ANCHO * e, cls.ALTO * e
        inset, ri, rd = cls.INSET * e, cls.RADIO_INVERSO * e, cls.RADIO_INFERIOR * e
        alto_p = cls.ALTO_PASTILLA * e
        mascara = Image.new("L", (ancho, alto), 0)
        dibujo = ImageDraw.Draw(mascara)
        # cuerpo: esquinas de abajo redondeadas y arriba cuadradas
        dibujo.rounded_rectangle([inset, 0, ancho - inset, alto_p], radius=rd, fill=255)
        dibujo.rectangle([inset, 0, ancho - inset, rd], fill=255)
        # codos invertidos: el cuadrado de la esquina menos su cuarto de círculo
        # (el arco sale hacia fuera y suelda la pastilla al borde de la pantalla)
        for izquierda in (True, False):
            x0 = inset - ri if izquierda else ancho - inset
            x1 = inset if izquierda else ancho - inset + ri
            dibujo.rectangle([x0, 0, x1, ri], fill=255)
            centro = (x0, ri) if izquierda else (x1, ri)
            dibujo.ellipse(
                [centro[0] - ri, centro[1] - ri, centro[0] + ri, centro[1] + ri],
                fill=0,
            )
        return mascara.resize((cls.ANCHO, cls.ALTO), Image.LANCZOS)

    @classmethod
    def _pastilla_base(cls):
        """La barra negra ya recortada: también es la misma en todos los frames."""
        cache = cls._base_cache
        if "imagen" in cache:
            return cache["imagen"]
        capa = Image.new("RGB", (cls.ANCHO, cls.ALTO), cls.CLAVE_RGB)
        capa.paste(cls.NEGRO, mask=cls._mascara_pastilla())
        cache["imagen"] = capa
        return capa

    def _pintar(self) -> None:
        """Dibuja la pastilla negra de la barra y los 5 puntos de frecuencia."""
        if Image is None or ImageDraw is None:
            return self._pintar_tk()

        capa = self._pastilla_base().copy()

        # Se pinta grande y se reduce: puntos y halos quedan sin escalones.
        escala = self.ESCALA_RENDER
        imagen = Image.new(
            "RGB",
            (self.ANCHO_PUNTOS * escala, self.ALTO_PUNTOS * escala),
            self.NEGRO,
        )
        dibujo = ImageDraw.Draw(imagen)
        centro_x = self.ANCHO_PUNTOS * escala / 2
        centro_y = self.ALTO_PUNTOS * escala / 2

        self._pintar_puntos(dibujo, self._puntos(), centro_x, centro_y, escala)
        if self.estado_actual == "pausado":
            self._pintar_pausa(dibujo, centro_x, centro_y, escala)

        imagen = imagen.resize((self.ANCHO_PUNTOS, self.ALTO_PUNTOS), Image.BOX)
        capa.paste(
            imagen,
            ((self.ANCHO - self.ANCHO_PUNTOS) // 2,
             (self.ALTO_PASTILLA - self.ALTO_PUNTOS) // 2),
        )
        ppm = b"P6 %d %d 255 " % (self.ANCHO, self.ALTO) + capa.tobytes("raw", "RGB")
        try:
            foto = tk.PhotoImage(data=ppm)
            self.lienzo.itemconfig(self.sprite, image=foto)
        except tk.TclError:
            self._pintar_tk()
            return
        self._foto = foto

    @classmethod
    def _silueta_pastilla(cls, muestras: int = 8) -> list[float]:
        """El contorno de la pastilla como polígono (para el respaldo sin Pillow)."""
        ancho, alto = cls.ANCHO, cls.ALTO
        inset, ri, rd = cls.INSET, cls.RADIO_INVERSO, cls.RADIO_INFERIOR
        alto_p = cls.ALTO_PASTILLA
        puntos: list[float] = [inset - ri, 0.0]

        def arco(cx, cy, r, a0, a1):
            for i in range(muestras + 1):
                a = math.radians(a0 + (a1 - a0) * i / muestras)
                puntos.extend((cx + r * math.cos(a), cy + r * math.sin(a)))

        arco(inset - ri, ri, ri, -90, 0)          # codo invertido izquierdo
        puntos.extend((inset, alto_p - rd))       # pared izquierda
        arco(inset + rd, alto_p - rd, rd, 180, 90)   # esquina inferior izquierda
        puntos.extend((ancho - inset - rd, alto_p))  # base
        arco(ancho - inset - rd, alto_p - rd, rd, 90, 0)  # esquina inferior derecha
        puntos.extend((ancho - inset, ri))        # pared derecha
        arco(ancho - inset + ri, ri, ri, 180, 270)   # codo invertido derecho
        return puntos

    def _pintar_tk(self) -> None:
        """Respaldo sin Pillow: la pastilla en polígono y los puntos en óvalos."""
        lienzo = self.lienzo
        lienzo.delete("grafico-orbe")
        lienzo.create_polygon(
            self._silueta_pastilla(), fill="#000000", outline="",
            tags="grafico-orbe",
        )
        centro_x, centro_y = self.cx, self.ALTO_PASTILLA // 2
        puntos = self._puntos()
        for factor, brillo in self.HALO:
            for x, y, radio, tinta in puntos:
                r = radio * factor
                color = tuple(int(c * brillo) for c in tinta)
                lienzo.create_oval(
                    centro_x + x - r, centro_y + y - r,
                    centro_x + x + r, centro_y + y + r,
                    fill=f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}",
                    outline="", tags="grafico-orbe",
                )
        if self.estado_actual == "pausado":
            gris = self.COLOR["pausado"]
            color = f"#{gris[0]:02x}{gris[1]:02x}{gris[2]:02x}"
            ancho, alto, hueco = 3.4, 11.0, 5.0
            y0 = centro_y + 14
            for lado in (-1, 1):
                xc = centro_x + lado * hueco / 2
                lienzo.create_rectangle(
                    xc - ancho / 2, y0, xc + ancho / 2, y0 + alto,
                    fill=color, outline="", tags="grafico-orbe",
                )

    @staticmethod
    def _oscurecer(color: str, factor: float) -> str:
        """Devuelve el color multiplicado (para halos y sombras)."""
        r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        return f"#{int(r * factor):02x}{int(g * factor):02x}{int(b * factor):02x}"

    @staticmethod
    def _aclarar(color: str, factor: float) -> str:
        """Acerca el color al blanco (brillo cuando oye tu voz)."""
        rojo, verde, azul = (int(color[i:i + 2], 16) for i in (1, 3, 5))
        return (
            f"#{int(rojo + (255 - rojo) * factor):02x}"
            f"{int(verde + (255 - verde) * factor):02x}"
            f"{int(azul + (255 - azul) * factor):02x}"
        )

    def _cadencia(self) -> int:
        """ms hasta el siguiente fotograma: rápido solo si algo se está moviendo.

        El reposo (o el silencio mientras escucha) se anima a 9 fotogramas por
        segundo en vez de 25: la respiración lenta se ve igual y la CPU baja
        bastante, que es lo que importa cuando la barra está quieta.
        """
        estado = self.estado_actual
        en_movimiento = estado in ("pensando", "hablando", "error")
        if estado == "escuchando":
            en_movimiento = (self.nivel_visible > 0.015
                             or abs(getattr(self, "orbita", 0.0)) > 0.01)
        return 40 if en_movimiento else 110

    def _tick(self) -> None:
        """Bucle de animación: lee la cola y redibuja el orbe."""
        try:
            while True:
                mensaje = self.cola.get_nowait()
                tipo, valor = mensaje[0], mensaje[1]

                if tipo == "estado":
                    self.estado_actual = valor
                    self.pulso = 0.0
                    if valor != "escuchando":
                        self.nivel_objetivo = 0.0
                elif tipo == "salir":
                    # Se sale ya mismo: ni se repinta ni se programa el
                    # siguiente fotograma (la ventana acaba de destruirse).
                    self.al_salir()
                    return
                else:  # progreso: nivel del micrófono, para el vúmetro
                    self.nivel_objetivo = valor
        except queue.Empty:
            pass

        # El nivel sube rápido y baja despacio, como un vúmetro de verdad
        destino = self.nivel_objetivo
        paso = 0.5 if destino > self.nivel_visible else 0.15
        self.nivel_visible += (destino - self.nivel_visible) * paso

        # El tiempo va con el reloj, no con la cadencia: si no, al ir más
        # lento en reposo la animación se ralentizaría sola.
        ahora = time.time()
        delta = min(0.3, ahora - self._ultimo_tick)
        self._ultimo_tick = ahora
        self.tiempo += delta
        self._pintar()

        self.root.after(self._cadencia(), self._tick)

    def run(self) -> None:
        self.root.mainloop()

    def cerrar(self) -> None:
        self.root.quit()
        self.root.destroy()


def probar_micros(segundos: float = 0.5) -> None:
    """Mide el volumen de cada entrada para descubrir cuál te oye de verdad."""
    import pyaudio

    micros = listar_microfonos()
    if not micros:
        log(f"{ROJO}No hay entradas de voz detectadas.{RESET}")
        return

    audio = _pyaudio()
    log(f"{AZUL}Midiendo {segundos:g} s de cada entrada…{RESET}")
    log(f"{GRIS}Habla o haz ruido durante la prueba para ver cuál responde.{RESET}\n")
    log(f"  {'idx':>4}  {'pico':>7}  entrada")

    for indice, nombre, predeterminado in micros:
        marca = "  (predeterminado de Windows)" if predeterminado else ""

        try:
            info = audio.get_device_info_by_index(indice)
            frecuencia = int(info.get("defaultSampleRate", 16000))
            tamano = max(256, int(frecuencia * 0.02))
            flujo = audio.open(
                rate=frecuencia, channels=1, format=pyaudio.paInt16, input=True,
                input_device_index=indice, frames_per_buffer=tamano,
            )
        except Exception:
            log(f"  {indice:>4}  {'-':>7}  {nombre}{marca} → no se puede abrir")
            continue

        pico = 0.0
        try:
            inicio = time.time()
            while time.time() - inicio < segundos:
                datos = flujo.read(tamano, exception_on_overflow=False)
                if datos:
                    pico = max(pico, OidoGoogle._energia(datos))
        except Exception:
            pass
        finally:
            flujo.stop_stream()
            flujo.close()

        if pico > 800:
            veredicto = "te oye bien"
        elif pico > 120:
            veredicto = "se oye bajo: habla más cerca o sube el volumen en Windows"
        elif pico > 20:
            veredicto = "casi no capta nada"
        else:
            veredicto = "mudo"

        log(f"  {indice:>4}  {pico:>7.0f}  {nombre}{marca} → {veredicto}")

    log(f"\n{GRIS}Quédate con el que se mueva: --micro <idx>, o elige con el ratón "
        f"en clic derecho sobre el orbe > Elegir micrófono…{RESET}")


def probar_nivel(indice: int | None = None) -> None:
    """Barra de nivel en vivo del micrófono: sirve para ver si te oye de verdad."""
    import pyaudio

    audio = _pyaudio()
    try:
        info = (
            audio.get_device_info_by_index(indice)
            if indice is not None else audio.get_default_input_device_info()
        )
    except Exception as error:
        log(f"{ROJO}No se puede usar el micrófono {indice}: {error}{RESET}")
        return

    frecuencia = int(info.get("defaultSampleRate", 16000))
    tamano = max(256, int(frecuencia * 0.02))

    try:
        flujo = audio.open(
            rate=frecuencia, channels=1, format=pyaudio.paInt16, input=True,
            input_device_index=indice, frames_per_buffer=tamano,
        )
    except Exception as error:
        log(f"{ROJO}No se puede abrir {nombre_micro(indice)}: {error}{RESET}")
        return

    log(f"{AZUL}Micrófono:{RESET} {nombre_micro(indice)}")
    log(f"{GRIS}Habla y mira la barra. Ctrl+C para salir.{RESET}")

    pico = 0.0
    try:
        while True:
            datos = flujo.read(tamano, exception_on_overflow=False)
            energia = OidoGoogle._energia(datos) if datos else 0.0
            pico = max(pico, energia)

            nivel = min(1.0, (energia / 2500) ** 0.5)
            llenos = int(nivel * 30)
            barra = "█" * llenos + "·" * (30 - llenos)
            print(f"\r  [{barra}] pico {pico:>6.0f}", end="", flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        flujo.stop_stream()
        flujo.close()

    if pico > 800:
        log(f"{VERDE}El micrófono te oye bien.{RESET}")
    elif pico > 120:
        log(f"{ROJO}Se oye muy bajo: acércate al micro o sube su volumen en Windows.{RESET}")
    else:
        log(f"{ROJO}No ha captado voz: puede estar silenciado en Windows o ser otra entrada.{RESET}")


class MedidorNivel:
    """Mide el volumen de un micrófono para ver cuál te está oyendo.

    Lee desde el hilo de la ventana, en trocitos de 30 ms, en vez de tener un
    hilo propio: PortAudio se cae si se mezclan hilos e instancias.
    """

    def __init__(self, indice: int | None):
        self.indice = indice
        self.nivel = 0.0
        self.error = ""
        self.flujo = None
        self.tamano = 0

    def leer(self) -> float:
        """Nivel actual de 0 a 1 (abre el micrófono la primera vez que se llama)."""
        if self.error:
            return 0.0

        if self.flujo is None:
            self._abrir()
            if self.flujo is None:
                return 0.0

        try:
            datos = self.flujo.read(self.tamano, exception_on_overflow=False)
        except Exception as error:
            self.error = str(error)
            self.cerrar()
            return 0.0

        if datos:
            # Escala con raíz cuadrada: así un micro bajito mueve la barra igual
            self.nivel = min(1.0, (OidoGoogle._energia(datos) / 2500) ** 0.5)

        return self.nivel

    def _abrir(self) -> None:
        """Abre el dispositivo a su frecuencia nativa (WASAPI exige 48000, por ejemplo)."""
        try:
            import pyaudio

            audio = _pyaudio()
            info = (
                audio.get_device_info_by_index(self.indice)
                if self.indice is not None else audio.get_default_input_device_info()
            )
            frecuencia = int(info.get("defaultSampleRate", 16000))
            self.tamano = max(256, int(frecuencia * 0.03))

            self.flujo = audio.open(
                rate=frecuencia, channels=1, format=pyaudio.paInt16, input=True,
                input_device_index=self.indice, frames_per_buffer=self.tamano,
            )
        except Exception as error:
            self.error = str(error)
            self.flujo = None

    def cerrar(self) -> None:
        if self.flujo is None:
            return

        try:
            self.flujo.stop_stream()
            self.flujo.close()
        except Exception:
            pass

        self.flujo = None


class SelectorMicro:
    """Ventanita para elegir la entrada de voz viendo cuál te oye de verdad.

    Habla mientras está abierta: la barra se mueve solo con el micrófono que
    esté seleccionado, así sabes cuál es el bueno antes de guardarlo.
    """

    def __init__(self, padre, al_elegir, al_cerrar, indice_actual: int | None = None):
        self.al_elegir = al_elegir
        self.al_cerrar = al_cerrar
        self.microfonos = listar_microfonos()
        self.indice = indice_actual
        self.medidor: MedidorNivel | None = None
        self.cargando = True

        self.ventana = tk.Toplevel(padre)
        self.ventana.title("Entrada de voz")
        self.ventana.attributes("-topmost", True)
        self.ventana.configure(bg="#16191f")
        self.ventana.geometry(f"460x360+{(padre.winfo_screenwidth() - 460) // 2}+120")
        self.ventana.protocol("WM_DELETE_WINDOW", self.cerrar)

        tk.Label(
            self.ventana, text="Elige el micrófono con el que te oye",
            bg="#16191f", fg="#ffffff", font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w", padx=14, pady=(12, 2))

        tk.Label(
            self.ventana,
            text="Habla y mira la barra: solo se mueve con el seleccionado.",
            bg="#16191f", fg="#9aa4b2", font=("Segoe UI", 8),
        ).pack(anchor="w", padx=14, pady=(0, 8))

        self.lista = tk.Listbox(
            self.ventana, height=8, bg="#0e1116", fg="#d7dde5",
            selectbackground="#2f9ff5", selectforeground="#ffffff",
            highlightthickness=0, font=("Segoe UI", 9),
        )
        self.lista.pack(fill="x", padx=14)
        self.lista.bind("<<ListboxSelect>>", self._elegir_en_lista)

        # Barra de nivel del micrófono seleccionado
        self.barra = tk.Canvas(self.ventana, height=16, bg="#0e1116", highlightthickness=0)
        self.barra.pack(fill="x", padx=14, pady=10)
        self.relleno = self.barra.create_rectangle(0, 0, 0, 16, fill="#2ecc71", outline="")

        self.aviso = tk.Label(
            self.ventana, text="", bg="#16191f", fg="#9aa4b2",
            font=("Segoe UI", 8), wraplength=430, justify="left",
        )
        self.aviso.pack(anchor="w", padx=14)

        botones = tk.Frame(self.ventana, bg="#16191f")
        botones.pack(fill="x", padx=14, pady=12)

        tk.Button(
            botones, text="Usar este micrófono", command=self._usar,
            bg="#2f9ff5", fg="#ffffff", relief="flat", padx=12, pady=6,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="left")

        tk.Button(
            botones, text="Cerrar", command=self.cerrar,
            bg="#2a3038", fg="#d7dde5", relief="flat", padx=12, pady=6,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=8)

        self._rellenar()
        self.cargando = False
        self.ventana.after(60, self._refrescar)

    def _rellenar(self) -> None:
        """Pone en la lista los micrófonos, marcando el que está en uso."""
        if not self.microfonos:
            self.aviso.config(text="No se ha detectado ningún micrófono. ¿Está conectado?")
            return

        seleccionar = 0
        for posicion, (indice, nombre, predeterminado) in enumerate(self.microfonos):
            etiqueta = nombre + ("  · predeterminado de Windows" if predeterminado else "")
            self.lista.insert("end", f"  {etiqueta}")
            if predeterminado and self.indice is None:
                seleccionar = posicion
            if self.indice is not None and indice == self.indice:
                seleccionar = posicion

        self.lista.selection_set(seleccionar)
        self.lista.see(seleccionar)
        self._abrir_medidor(self.microfonos[seleccionar][0])

    def _marcada(self) -> int | None:
        """Índice del dispositivo seleccionado en la lista."""
        seleccion = self.lista.curselection()
        if not seleccion or not self.microfonos:
            return None
        return self.microfonos[seleccion[0]][0]

    def _elegir_en_lista(self, _evento=None) -> None:
        if self.cargando:
            return
        indice = self._marcada()
        if indice is not None:
            self._abrir_medidor(indice)

    def _abrir_medidor(self, indice: int | None) -> None:
        """Deja de leer el micrófono anterior y empieza con el nuevo."""
        if self.medidor:
            self.medidor.cerrar()

        self.indice = indice
        self.medidor = MedidorNivel(indice)
        self.aviso.config(text=f"Escuchando: {nombre_micro(indice)}", fg="#9aa4b2")

    def _usar(self) -> None:
        if self.indice is None:
            self.aviso.config(text="Elige un micrófono de la lista primero.", fg="#e5484d")
            return

        try:
            self.al_elegir(self.indice)
        finally:
            # Aunque falle al guardar, hay que cerrar: si no, la escucha
            # se quedaría pausada para siempre sin motivo.
            self.cerrar()

    def _refrescar(self) -> None:
        """Dibuja la barra de nivel y los errores del dispositivo."""
        try:
            if not self.ventana.winfo_exists():
                return
        except tk.TclError:
            return   # la ventana ya se cerró

        ancho = max(1, self.barra.winfo_width())
        if self.medidor:
            self.medidor.leer()
            self.barra.coords(self.relleno, 0, 0, ancho * self.medidor.nivel, 16)
            if self.medidor.error:
                self.aviso.config(
                    text=f"No se puede abrir: {self.medidor.error[:90]}", fg="#e5484d",
                )
            elif self.medidor.nivel > 0.02:
                self.aviso.config(text="Te está oyendo ✔", fg="#2ecc71")

        self.ventana.after(40, self._refrescar)

    def cerrar(self) -> None:
        """Cierra la ventana y siempre avisa: la escucha no puede quedarse en pausa."""
        try:
            if self.medidor:
                self.medidor.cerrar()
            self.ventana.destroy()
        finally:
            self.al_cerrar()


def bucle_voz(chat: ChatCLI, oido, voz, orbe: Orbe, parar: threading.Event,
              pausa: threading.Event, mudo: bool = False,
              al_cerrar=None) -> None:
    """Bucle principal: escucha, pregunta al modelo y responde en voz alta."""
    pendiente = None
    ultimo_aviso = {"texto": "", "cuando": 0.0}
    avisado_pausa = False

    def mostrar_progreso(fase: str, texto: str, nivel: float) -> None:
        """Retroalimentación en vivo: el orbe late con tu voz."""
        if fase == "transcribiendo":
            orbe.estado("pensando")
            return

        ahora = time.time()
        # El audio llega cada 30 ms: redibujamos solo cuando cambia algo
        if texto == ultimo_aviso["texto"] and ahora - ultimo_aviso["cuando"] < 0.1:
            return

        ultimo_aviso["texto"], ultimo_aviso["cuando"] = texto, ahora
        orbe.progreso(nivel)

    try:
        while not parar.is_set():
            if pausa.is_set():
                if not avisado_pausa:
                    # Una sola vez: si no, el log se llena cada 200 ms
                    log(f"{GRIS}[pausa] en pausa: el orbe no escucha ni habla "
                        f"(clic en la barra para reanudar){RESET}")
                    avisado_pausa = True
                oido.descartar()
                orbe.estado("pausado")
                time.sleep(0.2)
                continue

            avisado_pausa = False
            orbe.estado("escuchando")
            ultimo_aviso["texto"] = ""

            texto = pendiente or oido.escuchar(parar, al_progreso=mostrar_progreso)
            pendiente = None
            orbe.progreso(0.0)

            if parar.is_set():
                break
            if not texto:
                # No ha entendido nada: seguimos escuchando sin molestar
                continue

            log(f"\n{AZUL}tú> {texto}{RESET}")

            if es_orden_de_cierre(texto):
                # Me apago yo: el vigilante de palmadas sigue ahí fuera y en
                # cuanto cierre el proceso volverá a prestarme el micrófono.
                log(f"{GRIS}[cierre] orden de apagado; dos palmadas me "
                    f"abrirán otra vez{RESET}")
                orbe.estado("hablando")
                try:
                    if not mudo and voz is not None:
                        voz.decir("Hasta luego.")
                        # decir() no bloquea: sin esperar, cerrar la ventana
                        # mataría la despedida en mitad de la frase.
                        voz.esperar()
                        time.sleep(0.2)
                except Exception as error:
                    log(f"{GRIS}[cierre] no pude despedirme ({error}){RESET}")
                finally:
                    # Aunque falle la voz hay que salir: si no, quedaría la
                    # ventana quieta con el bucle de escucha ya muerto.
                    if al_cerrar is not None:
                        al_cerrar()
                break

            orbe.estado("pensando")

            metricas = chat.preguntar(texto, imprimir=False)

            if metricas.error:
                log(f"{ROJO}Error: {metricas.error}{RESET}")
                orbe.estado("error")
                if not mudo:
                    if "email_verification" in metricas.error.lower() or "verify your email" in metricas.error.lower():
                        voz.decir("Hay que verificar el correo en el panel de Token Harbor antes de usar la API.")
                    elif "401" in metricas.error or "API_CHATGPT" in metricas.error:
                        voz.decir("La clave de la API no es válida o está desactivada. Revisa el archivo punto env.")
                    elif ("429" in metricas.error or "rate_limit" in metricas.error.lower()
                          or "quota" in metricas.error.lower()):
                        voz.decir("Se ha agotado la cuota gratuita por hoy. Vuelve a probar más tarde.")
                    else:
                        voz.decir("Ha habido un error al consultar el modelo. Mira la consola.")
                continue

            log(f"{metricas.texto}")
            log(f"{GRIS}[{metricas.resumen_corto()}]{RESET}")

            respuesta = limpiar_para_voz(metricas.texto)
            orbe.estado("hablando")

            if not mudo:
                log(f"{GRIS}[voz] diciendo ({len(respuesta)} caracteres){RESET}")
                voz.decir(respuesta)

                # Mientras habla, si le interrumpes pasamos directos a tu frase
                def interrumpido():
                    nueva = oido.frase_inmediata()
                    if nueva:
                        nonlocal pendiente
                        pendiente = nueva
                        voz.callar()
                        log(f"{GRIS}(interrumpido){RESET}")
                        return True
                    return False

                voz.esperar(al_paso=interrumpido)
                # La escucha de interrupción mantiene un stream abierto mientras
                # habla; lo soltamos antes de volver a la escucha normal.
                oido.descartar()
            else:
                time.sleep(0.2)

            orbe.estado("inactivo")
    except Exception as error:
        log(f"{ROJO}El modo voz se detuvo: {type(error).__name__}: {error}{RESET}")
        orbe.estado("error")
    finally:
        parar.set()


def generar_icono(destino: Path, centro: tuple = (127, 212, 255),
                  borde: tuple = (18, 70, 168)) -> Path:
    """Escribe un .ico con el thinking orb de orbe_art (varios tamaños).

    Si falta Pillow o numpy cae a la esfera dibujada a mano de 32 px, que no
    necesita dependencias.
    """
    try:
        import orbe_art
        return orbe_art.escribir_icono(destino, color=centro, borde=borde)
    except Exception as error:
        log(f"{GRIS}Arte fino no disponible ({type(error).__name__}); "
            f"icono de 32 px.{RESET}")

    lado = 32
    filas = []

    for y in range(lado - 1, -1, -1):  # los BMP van de abajo hacia arriba
        fila = bytearray()
        for x in range(lado):
            dx, dy = x - lado / 2 + 0.5, y - lado / 2 + 0.5
            distancia = math.hypot(dx, dy)
            radio = lado / 2 - 0.5

            if distancia > radio + 0.5:
                fila += bytes((0, 0, 0, 0))
                continue

            # Degradado del centro (claro) al borde (oscuro)
            proporcion = min(1.0, distancia / radio)
            rojo = int(centro[0] + (borde[0] - centro[0]) * proporcion)
            verde = int(centro[1] + (borde[1] - centro[1]) * proporcion)
            azul = int(centro[2] + (borde[2] - centro[2]) * proporcion)

            # Brillo arriba a la izquierda, como una bola de cristal
            brillo = max(0.0, 1 - math.hypot(dx + 8, dy + 8) / 10)
            rojo = int(rojo + (255 - rojo) * brillo * 0.85)
            verde = int(verde + (255 - verde) * brillo * 0.85)
            azul = int(azul + (255 - azul) * brillo * 0.85)

            # Suavizamos el borde para que no se vea dentado
            alfa = 255 if distancia < radio - 0.5 else int(255 * (radio + 0.5 - distancia))
            fila += bytes((max(0, min(255, azul)), max(0, min(255, verde)),
                           max(0, min(255, rojo)), max(0, min(255, alfa))))

        filas.append(bytes(fila))

    pixeles = b"".join(filas)
    mascara = bytes(lado * lado // 8)  # la máscara AND va vacía (usamos alfa)
    cabecera = struct.pack("<IiiHHIIiiII", 40, lado, lado * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    imagen = cabecera + pixeles + mascara

    icono = struct.pack("<HHH", 0, 1, 1)                          # cabecera ICO
    icono += struct.pack("<BBBBHHII", lado, lado, 0, 0, 1, 32, len(imagen), 22)
    icono += imagen

    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(icono)
    return destino


def crear_acceso_directo(
    script: Path | None = None,
    nombre: str = "Voz",
    descripcion: str = "Hablar con el modelo (orbe)",
    icono: Path | None = None,
    centro: tuple = (127, 212, 255),
    borde: tuple = (18, 70, 168),
    tecla: str = "CTRL+ALT+V",
) -> Path:
    """Crea en el escritorio un acceso directo que abre el orbe con su icono."""
    dibujo = generar_icono(icono or RAIZ / "assets" / "orbe.ico", centro, borde)
    enlace_script = script or RAIZ / "voz.py"

    shell = win32com.client.Dispatch("WScript.Shell")
    escritorio = Path(shell.SpecialFolders("Desktop"))

    # pythonw abre sin ventana de consola (el orbe es toda la interfaz)
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interprete = pythonw if pythonw.exists() else Path(sys.executable)

    enlace = shell.CreateShortCut(str(escritorio / f"{nombre}.lnk"))
    enlace.TargetPath = str(interprete)
    enlace.Arguments = f'"{enlace_script}"'
    enlace.WorkingDirectory = str(RAIZ)
    enlace.IconLocation = f"{dibujo},0"
    enlace.Description = descripcion
    if tecla:
        # Windows lanza el acceso directo desde cualquier ventana con esta combinación.
        enlace.Hotkey = tecla
    enlace.Save()

    return escritorio / f"{nombre}.lnk"


# --- Dos palmadas para abrir el agente ------------------------------------

VENTANA_AGENTE = "Voz"                # el título que le pone Orbe()
MUTEX_AGENTE = "Local\\AgenteOrbeVoz"
MUTEX_VIGILANTE = "Local\\AgentePalmadas"
ENLACE_PALMADAS = "Agente - palmadas"
_mutex_agente = 0                     # el proceso mantiene vivo su mutex
_mutex_vigilante = 0


def ventana_agente() -> int:
    """HWND de la ventana del orbe, o 0 si no está abierta."""
    try:
        return ctypes.windll.user32.FindWindowW(None, VENTANA_AGENTE)
    except Exception:
        return 0


def agente_vivo() -> bool:
    """True si ya hay un agente en marcha: ventana visible o proceso vivo."""
    if ventana_agente():
        return True
    kernel32 = ctypes.windll.kernel32
    manejador = kernel32.OpenMutexW(0x00100000, False, MUTEX_AGENTE)  # SYNCHRONIZE
    if manejador:
        kernel32.CloseHandle(manejador)
        return True
    return False


def marcar_agente_vivo() -> None:
    """El agente avisa de que existe para que el vigilante suelte el micro."""
    global _mutex_agente
    if not _mutex_agente:
        _mutex_agente = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_AGENTE)


def traer_al_frente(ventana: int | None = None) -> bool:
    """Muestra la ventana del agente que ya esté abierta. True si la había."""
    ventana = ventana or ventana_agente()
    if not ventana:
        return False
    user32 = ctypes.windll.user32
    user32.ShowWindow(ventana, 9)                 # SW_RESTORE
    user32.SetForegroundWindow(ventana)
    return True


def abrir_agente() -> bool:
    """Abre el agente (si ya estaba, solo trae la ventana al frente)."""
    if traer_al_frente():
        log(f"{GRIS}[palmadas][{_hora()}] el agente ya estaba abierto: ventana al frente{RESET}")
        return False

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interprete = pythonw if pythonw.exists() else Path(sys.executable)
    log(f"{AZUL}[palmadas][{_hora()}] dos palmadas: abriendo el agente{RESET}")
    subprocess.Popen([str(interprete), str(RAIZ / "orbe.py")], cwd=str(RAIZ))
    return True


def umbral_palmadas() -> float | None:
    """Umbral fijo de AGENTE_PALMADA_UMBRAL en el .env, o None para calibrar."""
    valor = os.getenv("AGENTE_PALMADA_UMBRAL", "").strip()
    if not valor:
        return None
    try:
        return float(valor)
    except ValueError:
        log(f"{GRIS}[palmadas] AGENTE_PALMADA_UMBRAL no es un número ({valor}): "
            f"se calibra al arrancar{RESET}")
        return None


def _pico_de_fondo(fuente, tamano: int, segundos: float = 1.0) -> float:
    """Pico máximo de un segundo: el ruido de la habitación por donde va."""
    fin = time.time() + segundos
    picos = []
    while time.time() < fin:
        picos.append(Palmadas._pico(fuente.stream.read(tamano)))
    # Una entrada atascada en un valor enorme daría un fondo de 32768 y un
    # umbral de 65536: con eso no entraría ni un grito, así que se ignora.
    return pico_sano(picos)


# Un micro mudo devuelve siempre lo mismo (cero, o el mismo valor atascado) y
# eso pico_sano lo deja en 0; por debajo de esto la entrada no sirve. El corte
# va en 1 y no en 50 porque hay micrófonos con compuerta (el MHW-100) que en
# una habitación callada susurran 2 o 3: con el corte viejo se tomaban por
# mudos y el vigilante se iba a escuchar a otro micro.
MICRO_MUERTO = 1.0


def pico_sano(picos: list[float]) -> float:
    """Pico de una sonda; 0 si la entrada está muda o atascada.

    Hay dispositivos que devuelven siempre el mismo valor enorme (una captura
    rota o ya abierta en exclusiva): eso no es sonido y no sirve para calibrar.
    """
    if len(set(picos)) < 2:
        return 0.0
    return max(picos, default=0.0)


def _pico_de_sondeo(indice: int | None, segundos: float = 1.0) -> float:
    """Pico que capta un micro abierto un rato. Lanza si no se puede abrir.

    Un segundo entero: hace falta para que un micro sano en una habitación
    callada llegue a soltar algún pico, mientras que uno mudo sigue mudo.
    """
    import speech_recognition as sr

    trozo = 0.05
    with sr.Microphone(device_index=indice) as fuente:
        tamano = max(2, int(fuente.SAMPLE_RATE * trozo)) * 2
        cantidad = max(2, int(segundos / trozo))
        picos = [Palmadas._pico(fuente.stream.read(tamano)) for _ in range(cantidad)]
    return pico_sano(picos)


def _candidatos_micro(indice: int | None) -> list:
    """El micro pedido primero, luego el de Windows y después los micrófonos."""
    microfonos = listar_microfonos()
    nombres = {numero: nombre for numero, nombre, _ in microfonos}
    predeterminado = next((numero for numero, _, es_defecto in microfonos if es_defecto), None)
    orden = [indice]
    if predeterminado != indice:
        orden.append(predeterminado)
    resto = [numero for numero, _, _ in microfonos if numero not in orden]
    # Las entradas genéricas («Input», «Asignador»…) suelen no ser un micro de
    # verdad: si hay nombres con cara de micrófono, esos van primero.
    con_carita = [numero for numero in resto if "mic" in nombres.get(numero, "").lower()]
    return orden + (con_carita or resto)


def elegir_micro_palmadas(indice: int | None, sondeo, candidatos: list | None = None):
    """Micro con el que vigilar: el pedido si capta, si no el siguiente vivo.

    El micrófono configurado puede estar mudo (por ejemplo, los cascos
    desconectados): en ese caso se prueban las demás entradas para que las
    palmadas sigan abriendo el agente en lugar de quedarse sordo.
    """
    if candidatos is None:
        candidatos = _candidatos_micro(indice)
    for numero in candidatos:
        try:
            if sondeo(numero) >= MICRO_MUERTO:
                return numero
        except Exception:
            continue            # ni se puede abrir: pasa al siguiente
    return indice               # ninguno capta: el de siempre y a calibrar


# El detector decide con trozos de 20 ms, pero se leen de 100 en 100 ms: la
# mitad de lecturas al driver de audio con la misma precisión.
TROZO_PALMADAS = 0.02
GRUPO_PALMADAS = 0.10


COOLDOWN_PALMADAS = 8.0     # segundos que se ignora el micro tras abrir o cerrar


class ReposoPalmadas:
    """No hace caso al micro un rato después de abrir o cerrar el agente.

    Al cerrar con el ratón, los dos clics (el derecho y el de «Salir»)
    llegan al micrófono como dos golpes secos con la distancia justa: el
    detector los tomaba por palmadas y el agente se volvía a abrir solo.
    Lo mismo con el ruido del arranque, así que tras cada apertura y cada
    cierre se ignora el micro un rato.
    """

    def __init__(self, segundos: float = COOLDOWN_PALMADAS) -> None:
        self.segundos = segundos
        self.vivo = False
        self.hasta = 0.0

    def observar(self, vivo: bool, ahora: float | None = None) -> None:
        """Anota lo que hay ahora: si acaba de cerrarse, empieza el reposo."""
        ahora = time.time() if ahora is None else ahora
        if vivo:
            self.vivo = True
        elif self.vivo:
            self.vivo = False
            self.hasta = ahora + self.segundos

    def abrir(self, ahora: float | None = None) -> None:
        """Acabamos de abrir el agente: margen por si arranca y se cae ya."""
        ahora = time.time() if ahora is None else ahora
        self.hasta = max(self.hasta, ahora + self.segundos)

    def puede_escuchar(self, ahora: float | None = None) -> bool:
        """True cuando ya ha pasado el reposo y se puede volver a escuchar."""
        return (time.time() if ahora is None else ahora) >= self.hasta


def _hora() -> str:
    """Hora local con segundos: para ordenar los mensajes del vigilante."""
    return time.strftime("%H:%M:%S")


def _escuchar_palmadas(indice_micro: int | None, umbral: float | None) -> bool:
    """Abre el micro, calibra y espera; sale al oír dos palmadas o al ver al agente.

    True si ha detectado y abierto el agente (para que el vigilante marque
    su reposo); False si el agente se abrió por otro medio.
    """
    import speech_recognition as sr

    with sr.Microphone(device_index=indice_micro) as fuente:
        detector = Palmadas(umbral)
        frecuencia = fuente.SAMPLE_RATE
        bytes_trozo = max(2, int(frecuencia * TROZO_PALMADAS)) * 2
        bytes_grupo = max(bytes_trozo, int(frecuencia * GRUPO_PALMADAS) * 2)
        bytes_grupo -= bytes_grupo % bytes_trozo          # múltiplo exacto de trozos
        detector.calibrar(_pico_de_fondo(fuente, bytes_grupo // 2))
        umbral = max(detector.pico_minimo, detector.fondo * Palmadas.FACTOR_FONDO)
        log(f"{GRIS}[palmadas][{_hora()}] escuchando: fondo {detector.fondo:.0f}, "
            f"umbral {umbral:.0f}{RESET}")

        detectado = False
        contados = 0
        while not agente_vivo() and not detectado:
            datos = fuente.stream.read(bytes_grupo // 2)
            # Cada trozo lleva su instante (el del audio real, no el del reloj
            # de pared): así el detector ve los 20 ms de siempre.
            base = time.time() - (len(datos) / 2) / frecuencia
            for i in range(0, len(datos), bytes_trozo):
                fuerte = detector.alimentar(datos[i:i + bytes_trozo],
                                            ahora=base + (i / 2) / frecuencia)
                # Cada golpe que pasa se apunta: así se ve qué ruido dispara
                while len(detector.historial) > contados:
                    golpe = detector.historial[contados]
                    contados += 1
                    log(f"{GRIS}[palmadas][{_hora()}] golpe: pico {golpe['pico']}, "
                        f"{golpe['duracion'] * 1000:.0f} ms (umbral {umbral:.0f}){RESET}")
                if fuerte:
                    detectado = True
                    break

        if detectado and detector.deteccion:
            d = detector.deteccion
            log(f"{AZUL}[palmadas][{_hora()}] pareja: {d['pico1']} y {d['pico2']} "
                f"a {d['distancia']} s (umbral {umbral:.0f}){RESET}")

        if not detectado:
            return False    # el agente se abrió por otro medio: el micro es suyo

    # Ya fuera del `with`: el micro queda libre antes de abrir nada.
    abierto = abrir_agente()
    if abierto:
        time.sleep(5.0)     # margen para que arranque y reclame el micrófono
    return abierto


def _bajar_prioridad() -> None:
    """Que el vigilante trabaje por debajo: nunca le quita CPU al orbe."""
    try:
        kernel32 = ctypes.windll.kernel32
        proceso = kernel32.GetCurrentProcess()
        kernel32.SetPriorityClass(proceso, 0x00004000)   # BELOW_NORMAL_PRIORITY
    except Exception:
        pass


def vigilar_palmadas(indice_micro: int | None = None) -> None:
    """Escucha dos palmadas en segundo plano para abrir el agente.

    El micro solo se abre mientras el agente no está en marcha; no transcribe,
    no guarda ni manda audio: solo mide picos en local.
    """
    global _mutex_vigilante
    _bajar_prioridad()
    kernel32 = ctypes.windll.kernel32
    if not _mutex_vigilante:
        _mutex_vigilante = kernel32.CreateMutexW(None, False, MUTEX_VIGILANTE)
        if kernel32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            log(f"{GRIS}[palmadas] ya hay un vigilante de palmadas en marcha{RESET}")
            return
    umbral = umbral_palmadas()
    log(f"{AZUL}[palmadas] dos palmadas abren el agente "
        f"(umbral {'fijo ' + format(umbral, '.0f') if umbral else 'calibrado'}){RESET}")
    usado = indice_micro                     # el preferido puede estar mudo hoy
    reposo = ReposoPalmadas()
    reposo_logueado = False
    while True:
        vivo = agente_vivo()
        reposo.observar(vivo)                # si acaba de cerrarse, cuenta el reposo
        if vivo:
            time.sleep(1.0)                     # el micro lo usa el agente
            continue
        if not reposo.puede_escuchar():
            # Recién cerrado o recién abierto: los clics y el ruido del
            # arranque parecen palmadas, así que se ignoran unos segundos.
            if not reposo_logueado:
                log(f"{GRIS}[palmadas][{_hora()}] reposo {reposo.segundos:g} s tras "
                    f"abrir/cerrar: ignoro el micro{RESET}")
                reposo_logueado = True
            time.sleep(0.5)
            continue
        reposo_logueado = False
        elegido = elegir_micro_palmadas(indice_micro, _pico_de_sondeo)
        if elegido != usado:
            log(f"{ROJO}[palmadas] {nombre_micro(indice_micro)} no capta nada: "
                f"vigilo con {nombre_micro(elegido)}{RESET}")
            usado = elegido
        try:
            if _escuchar_palmadas(elegido, umbral):
                reposo.abrir()               # acaba de abrirse: margen por si cae
        except Exception as error:
            log(f"{ROJO}[palmadas] {type(error).__name__}: {error} "
                f"(otro intento en 5 s){RESET}")
            time.sleep(5.0)


def carpeta_inicio_sesion() -> Path:
    """Carpeta de inicio de sesión de Windows (se ejecuta al entrar el usuario)."""
    shell = win32com.client.Dispatch("WScript.Shell")
    return Path(shell.SpecialFolders("Startup"))


def instalar_palmadas(carpeta: Path | None = None) -> Path:
    """Deja el vigilante de palmadas en el inicio de sesión de Windows."""
    destino = (carpeta or carpeta_inicio_sesion()) / f"{ENLACE_PALMADAS}.lnk"
    shell = win32com.client.Dispatch("WScript.Shell")
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interprete = pythonw if pythonw.exists() else Path(sys.executable)
    enlace = shell.CreateShortCut(str(destino))
    enlace.TargetPath = str(interprete)
    enlace.Arguments = f'"{RAIZ / "orbe.py"}" --vigilar-palmadas'
    enlace.WorkingDirectory = str(RAIZ)
    enlace.IconLocation = f"{RAIZ / 'assets' / 'orbe-agente.ico'},0"
    enlace.Description = "Dos palmadas para abrir el agente"
    enlace.Save()
    return destino


def desinstalar_palmadas(carpeta: Path | None = None) -> bool:
    """Quita el vigilante del inicio de sesión. True si había algo que quitar."""
    destino = (carpeta or carpeta_inicio_sesion()) / f"{ENLACE_PALMADAS}.lnk"
    if destino.exists():
        destino.unlink()
        return True
    return False


def _arrancar_vigilante(indice_micro: int | None = None) -> None:
    """Lanza el vigilante para esta sesión (si ya hay otro, este se sale solo)."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interprete = pythonw if pythonw.exists() else Path(sys.executable)
    orden = [str(interprete), str(RAIZ / "orbe.py"), "--vigilar-palmadas"]
    if indice_micro is not None:
        orden += ["--micro", str(indice_micro)]
    subprocess.Popen(orden, cwd=str(RAIZ), creationflags=subprocess.CREATE_NO_WINDOW)


def detener_vigilante() -> int:
    """Cierra los procesos que están con --vigilar-palmadas. Devuelve cuántos."""
    if not SAPI_DISPONIBLE:
        return -1
    localizador = win32com.client.Dispatch("WbemScripting.SWbemLocator")
    servicios = localizador.ConnectServer(".", r"root\cimv2")
    consulta = ("SELECT ProcessId, CommandLine FROM Win32_Process "
                "WHERE CommandLine LIKE '%--vigilar-palmadas%'")
    cerrados = 0
    fallos = []
    for proceso in servicios.ExecQuery(consulta):
        pid = int(proceso.ProcessId)
        if pid == os.getpid():
            continue
        try:
            # Ojo con proceso.Terminate: con el despacho dinámico ese
            # atributo ya mata el proceso y devuelve el código, y al llamarlo
            # encima solo salía un TypeError que escondía el éxito.
            os.kill(pid, signal.SIGTERM)      # en Windows: TerminateProcess
            cerrados += 1
        except Exception as error:
            fallos.append(f"{pid} ({error})")
    if fallos:
        log(f"{ROJO}No se pudo detener: {', '.join(fallos)}{RESET}")
    return cerrados


def atender_palmadas(args, indice_micro: int | None) -> bool:
    """Órdenes de las palmadas. True si ya hizo su trabajo y hay que salir."""
    if args.detener_palmadas:
        cerrados = detener_vigilante()
        if cerrados < 0:
            log(f"{ROJO}Para el vigilante de palmadas necesitas pywin32: "
                f"pip install pywin32{RESET}")
        elif cerrados:
            log(f"{AZUL}Vigilante de palmadas detenido ({cerrados}){RESET}")
        else:
            log(f"{GRIS}No había ningún vigilante de palmadas en marcha{RESET}")
        return True

    if args.desinstalar_palmadas:
        if desinstalar_palmadas():
            log(f"{AZUL}Vigilante de palmadas quitado del inicio de sesión{RESET}")
        else:
            log(f"{GRIS}El vigilante de palmadas no estaba instalado{RESET}")
        return True

    if args.instalar_palmadas:
        if not SAPI_DISPONIBLE:
            log(f"{ROJO}Para instalar el vigilante necesitas pywin32: "
                f"pip install pywin32{RESET}")
            return True
        try:
            ruta = instalar_palmadas()
        except Exception as error:
            log(f"{ROJO}No se pudo crear el acceso directo: "
                f"{type(error).__name__}: {error}{RESET}")
            return True
        log(f"{AZUL}Dos palmadas abrirán el agente:{RESET} {ruta}")
        log(f"{GRIS}Arranca con Windows; de momento queda escuchando ya mismo.{RESET}")
        _arrancar_vigilante(indice_micro)
        return True

    if args.vigilar_palmadas:
        vigilar_palmadas(indice_micro)      # bloquea hasta que se cierre
        return True

    return False


def atender_microfono(args, indice_micro: int | None) -> bool:
    """Órdenes que solo miran el micrófono. True si ya hizo su trabajo y hay que salir."""
    if args.listar_micros:
        micros = listar_microfonos()
        log(f"{AZUL}Entradas de voz detectadas:{RESET}")
        for indice, nombre, predeterminado in micros:
            marca = f"{VERDE}  <- predeterminado de Windows{RESET}" if predeterminado else ""
            log(f"  {indice:>3}  {nombre}{marca}")
        log(f"{GRIS}Para fijarlo: --micro <número>, o elige con el ratón en "
            f"clic derecho sobre el orbe > Elegir micrófono…{RESET}")
        return True

    if args.probar_micros:
        probar_micros()
        return True

    if args.nivel:
        probar_nivel(indice_micro)
        return True

    return False


def crear_parser(descripcion: str = "Habla con el modelo usando el orbe.") -> argparse.ArgumentParser:
    """Los flags del modo voz, que también usa quien reutilice el orbe."""
    parser = argparse.ArgumentParser(description=descripcion)
    parser.add_argument("-m", "--modelo", default=MODELO_DEFECTO, help="Modelo a usar")
    parser.add_argument("--oido", choices=["auto", "sapi", "google", "teclado"], default="auto",
                        help="De dónde viene tu voz (por defecto, reconocimiento en español)")
    parser.add_argument("--idioma", default="es-ES",
                        help="Idioma del reconocimiento de voz (por defecto: es-ES)")
    parser.add_argument("--silencio", type=float, default=0.7,
                        help="Segundos de silencio para dar por terminada tu frase (0.7)")
    parser.add_argument("--transcripcion", choices=["auto", "whisper", "google"],
                        default="auto",
                        help="Motor de transcripción: whisper local, Google o auto (auto)")
    parser.add_argument("--modelo-whisper", default="small",
                        help="Modelo de faster-whisper: tiny, base, small… (small)")
    parser.add_argument("--texto", action="store_true", help="Escribir en consola en vez de hablar")
    parser.add_argument("--voz-salida", default=None,
                        help="Voz de salida: nombre SAPI (p. ej. Helena), voz Edge o ID de Fish Audio")
    parser.add_argument("--voz-motor", choices=["edge", "sapi", "fish"], default=None,
                        help="Motor de salida: edge (Edge TTS, neural), sapi (Windows) o fish (Fish Audio)")
    parser.add_argument("--velocidad", type=int, default=0, help="Velocidad de la voz (-10 a 10)")
    parser.add_argument("--volumen", type=int, default=100, help="Volumen de la voz (0 a 100)")
    parser.add_argument("--mudo", action="store_true", help="No hablar en voz alta")
    parser.add_argument("--no-fallback", action="store_true",
                        help="No cambiar de modelo si el elegido no está disponible")
    parser.add_argument("--micro", default=None,
                        help="Micrófono a usar: número de la lista o trozo del nombre")
    parser.add_argument("--listar-micros", action="store_true",
                        help="Muestra las entradas de voz disponibles y sale")
    parser.add_argument("--probar-micros", action="store_true",
                        help="Mide el volumen de cada entrada y sale (para saber cuál te oye)")
    parser.add_argument("--nivel", action="store_true",
                        help="Barra de nivel en vivo del micrófono elegido y sale")
    parser.add_argument("--acceso-directo", action="store_true",
                        help="Crea el icono del orbe en el escritorio y sale")
    parser.add_argument("--tecla", default=None,
                        help="Atajo del acceso directo, por ejemplo CTRL+ALT+A")
    parser.add_argument("--instalar-palmadas", action="store_true",
                        help="Dos palmadas abren el agente: lo deja en el inicio de sesión")
    parser.add_argument("--desinstalar-palmadas", action="store_true",
                        help="Quita el vigilante de palmadas del inicio de sesión")
    parser.add_argument("--detener-palmadas", action="store_true",
                        help="Detiene el vigilante de palmadas que esté en marcha")
    parser.add_argument("--vigilar-palmadas", action="store_true",
                        help="Escucha dos palmadas para abrir el agente (solo esta sesión)")

    return parser


def main() -> None:
    args = crear_parser().parse_args()

    configurar_salida()

    # Micrófono: lo que diga el comando, o el que guardaste la última vez
    micro_pedido = args.micro if args.micro is not None else leer_config().get("micro")
    indice_micro = resolver_micro(micro_pedido)

    if atender_palmadas(args, indice_micro):
        return

    if atender_microfono(args, indice_micro):
        return

    if not SAPI_DISPONIBLE and not args.texto and args.oido != "google":
        log(f"{ROJO}Para el modo voz necesitas pywin32: pip install pywin32{RESET}")
        sys.exit(1)

    if args.acceso_directo:
        ruta = crear_acceso_directo(tecla=args.tecla or "CTRL+ALT+V")
        log(f"{AZUL}Acceso directo creado:{RESET} {ruta}")
        return

    chat = ChatCLI(
        modelo=args.modelo,
        usar_color=False,
        respaldo=None if args.no_fallback else MODELO_RESPALDO,
    )

    arrancar(chat, args, indice_micro)


def arrancar(chat, args, indice_micro: int | None = None, preparar_chat=None) -> None:
    """Monta el orbe, los oídos y la voz alrededor del cerebro que se le pase.

    Basta con que `chat` tenga `modelo` y `preguntar(texto, imprimir=...)`, así
    que sirve igual el chat de consola que el agente con memoria.
    """
    # El vigilante de palmadas solo usa el micro mientras no hay agente: aquí
    # se marca su presencia para que lo suelte en cuanto arranque.
    if agente_vivo():
        # Ya hay otro orbe en marcha: no se abren dos ventanas apiladas (si
        # hubiera dos, cerrar una dejaría la otra encima como si se reabriera).
        traer_al_frente()
        log(f"{GRIS}[agente] ya había otro agente abierto: no abro una copia{RESET}")
        return
    marcar_agente_vivo()

    # El oído se crea dentro del hilo, así que el menú lo mira de aquí
    caja: dict = {"oido": None}
    chat_actual: dict = {"valor": chat}

    parar = threading.Event()
    pausa = threading.Event()

    def alternar_pausa() -> None:
        if pausa.is_set():
            pausa.clear()
            log(f"{VERDE}[pausa] escucha reanudada{RESET}")
        else:
            pausa.set()
            log(f"{GRIS}[pausa] escucha pausada (clic en la barra para reanudar){RESET}")

    def salir() -> None:
        parar.set()
        orbe.cerrar()

    def cerrar_por_voz() -> None:
        """La orden «ciérrate, Jarvis»: cierre desde el hilo de voz vía cola."""
        orbe.pedir_cierre()

    def elegir_micro() -> None:
        """Abre el selector de entrada de voz (el orbe sigue en el hilo de Tk)."""
        oido = caja["oido"]
        if oido is None:
            log(f"{GRIS}El oído aún se está preparando…{RESET}")
            return

        estaba_pausado = pausa.is_set()
        pausa.set()      # solo un programa puede leer el micro a la vez
        oido.abortar()

        def al_elegir(indice: int) -> None:
            oido.cambiar_micro(indice)
            guardar_config(micro=indice)
            log(f"{AZUL}Micrófono guardado:{RESET} {nombre_micro(indice)}")

        def al_cerrar() -> None:
            if not estaba_pausado:
                pausa.clear()

        try:
            SelectorMicro(
                orbe.root, al_elegir, al_cerrar,
                indice_actual=getattr(oido, "indice_micro", None),
            )
        except Exception as error:
            # Si el selector no llega a abrirse no podemos quedarnos en pausa:
            # el orbe dejaría de escuchar sin que se note por qué.
            al_cerrar()
            log(f"{ROJO}No se pudo abrir el selector de micrófono: {error}{RESET}")

    orbe = Orbe(al_click=alternar_pausa, al_salir=salir, al_elegir_micro=elegir_micro)
    orbe.estado("inactivo")

    def hilo_voz() -> None:
        """Todo el trabajo pesado va aquí; el orbe vive en el hilo principal."""
        try:
            chat_activo = preparar_chat() if preparar_chat is not None else chat
            chat_actual["valor"] = chat_activo

            oido = OidoTeclado() if args.texto else crear_oido(
                args.oido, args.silencio, indice_micro, args.idioma,
                args.transcripcion, args.modelo_whisper
            )
            caja["oido"] = oido
            if args.mudo:
                voz = None
            else:
                motor_voz = (args.voz_motor or os.getenv("VOZ_MOTOR", "sapi")).lower()
                if motor_voz == "fish":
                    voz = VozFishAudio(args.voz_salida, args.velocidad, args.volumen)
                elif motor_voz == "edge":
                    try:
                        voz = VozEdge(args.voz_salida, args.velocidad, args.volumen)
                    except Exception as error:
                        log(f"{GRIS}Edge TTS no disponible ({error}); "
                            f"uso la voz de Windows.{RESET}")
                        voz = Voz(args.voz_salida or "Helena", args.velocidad, args.volumen)
                else:
                    voz = Voz(args.voz_salida or "Helena", args.velocidad, args.volumen)

            log(f"{AZUL}Modo voz listo.{RESET} Modelo: {chat_activo.modelo}")
            log(f"{GRIS}Oído: {oido.descripcion}" + (f" | Voz: {voz.nombre}" if voz else " | mudo"))
            log(f"La barra de arriba refleja lo que hace el orbe: escucha, piensa y habla.")
            log(f"Se calla {args.silencio:g} s tras tu última palabra y entonces te responde.")
            log(f"Clic en el orbe para pausar; clic derecho para salir.{RESET}")

            bucle_voz(chat_activo, oido, voz, orbe, parar, pausa, mudo=args.mudo,
                      al_cerrar=cerrar_por_voz)
        except (Exception, SystemExit) as error:
            log(f"{ROJO}Error al arrancar el modo voz: {type(error).__name__}: {error}{RESET}")
            orbe.estado("error")
        finally:
            parar.set()

    threading.Thread(target=hilo_voz, daemon=True).start()

    try:
        orbe.run()
    except KeyboardInterrupt:
        pass
    finally:
        parar.set()
        cerrar = getattr(chat_actual["valor"], "cerrar_sesion", None)
        if cerrar:
            cerrar()


if __name__ == "__main__":
    main()
