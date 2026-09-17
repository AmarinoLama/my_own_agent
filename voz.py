"""Modo voz: habla con el modelo y un orbe flotante te dice qué está pasando.

El orbe se queda siempre encima de todo, arriba en el centro de la pantalla, y
cambia de color según el estado: escuchando (azul), pensando (ámbar), hablando
(verde), en espera (gris) o error (rojo). Arrástralo con el ratón para moverlo,
haz clic para pausar o reanudar la escucha y clic derecho para el menú.

    python voz.py                       # hablar por micrófono
    python voz.py --texto               # escribir en la consola (sin micro)
    python voz.py --acceso-directo      # crea el icono en el escritorio
    python voz.py --modelo mistralai/mistral-large-2512

Escuchar usa el reconocedor de voz de Windows (offline, en español) y hablar usa
SAPI, así que no hay que instalar nada: solo necesitas pywin32, que ya tienes.
"""

import argparse
import json
import math
import queue
import re
import struct
import sys
import threading
import time
import tkinter as tk
import os
import urllib.error
import urllib.request
from pathlib import Path

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
        # flush: si no, al haber una ventana de por medio la consola se queda a medias
        print(texto, flush=True)
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
                if preferida.lower() in voz.GetDescription().lower():
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


class OidoGoogle:
    """Oído con SpeechRecognition + Google, con detección propia de voz.

    Google no devuelve texto mientras hablas, así que leemos el micrófono
    nosotros mismos por trocitos: medimos el nivel (para que el orbe lo refleje),
    vemos cuándo empiezas a hablar y cuándo callas, y solo entonces mandamos el
    audio a transcribir. Así responde en cuanto te callas, sin esperas raras.
    """

    def __init__(self, idioma: str = "es-ES", silencio_final: float = 0.7,
                 micro: int | None = None):
        import speech_recognition as sr

        self.sr = sr
        self.idioma = idioma
        self.silencio_final = silencio_final  # segundos de silencio = has acabado
        self.indice_micro = micro             # None = el predeterminado de Windows
        self.pedido_cambio: int | None = None
        self.abortar_escucha = False
        self.parcial = ""
        self.nivel = 0.0
        self.umbral_maximo = 6000.0
        self.minimo_bloques = 6               # menos de esto es un golpe, no una frase

        # Escucha de interrupción mientras el agente está hablando (barge-in).
        # Se mantiene un único stream abierto durante esa fase y se cierra al
        # terminar, para no competir con la escucha normal del mismo micrófono.
        self._interrupcion_fuente = None
        self._interrupcion_audio = bytearray()
        self._interrupcion_hablando = False
        self._interrupcion_silencio_desde = None
        self._interrupcion_ultimo_error = None

        self.reconocedor = sr.Recognizer()
        self._abrir_micro()

    def _abrir_micro(self) -> None:
        """Abre el micrófono elegido y mide el ruido de fondo para el umbral."""
        try:
            self.micro = self.sr.Microphone(device_index=self.indice_micro)
        except Exception as error:
            log(f"{ROJO}No se pudo abrir el micrófono {self.indice_micro} ({error}); "
                f"uso el predeterminado{RESET}")
            self.indice_micro = None
            self.micro = self.sr.Microphone()

        self.descripcion = f"Google ({self.idioma}) · {nombre_micro(self.indice_micro)}"

        # Trocitos de 30 ms, los mismos que usa SpeechRecognition por dentro
        self.tamano_bloque = int(self.micro.SAMPLE_RATE * 0.03) * self.micro.SAMPLE_WIDTH

        with self.micro as fuente:
            self.reconocedor.adjust_for_ambient_noise(fuente, duration=0.6)

        # El umbral sale del ruido de fondo medido al arrancar
        self.umbral_minimo = max(150.0, self.reconocedor.energy_threshold * 1.15)
        self.umbral = self.umbral_minimo
        self.escala = self.umbral * 5   # nivel 1.0 cuando hablas clarito

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
        hablando = False
        silencio_desde = None
        audio = bytearray()

        with self.micro as fuente:
            while not parar.is_set():
                # Cambio de micrófono o pausa: salimos y soltamos el dispositivo
                if self.abortar_escucha:
                    self.abortar_escucha = False
                    return None

                datos = fuente.stream.read(self.tamano_bloque)
                energia = self._energia(datos)
                self.nivel = min(1.0, energia / self.escala) if energia else 0.0

                if not hablando:
                    # Todavía no habla: avisamos del nivel y seguimos esperando
                    if self.nivel < 0.2:
                        if al_progreso:
                            al_progreso("oido", "", self.nivel)
                        if espera_max and time.time() - inicio > espera_max:
                            return None
                        continue

                    hablando = True
                    audio = bytearray(datos)
                else:
                    audio += datos

                    if self.nivel < 0.2:
                        if silencio_desde is None:
                            silencio_desde = time.time()
                        elif time.time() - silencio_desde >= self.silencio_final:
                            break  # ha callado: ya podemos transcribir
                    else:
                        silencio_desde = None

                if al_progreso:
                    al_progreso("oido", "", self.nivel)

        self.nivel = 0.0

        # Un parpadeo de ruido no es una frase: subimos el umbral para no
        # dispararse con el ruido de fondo (ventilador, música, teclado...)
        if len(audio) < self.tamano_bloque * self.minimo_bloques:
            self._ajustar_umbral(1.15)
            return None

        if al_progreso:
            al_progreso("transcribiendo", "", 0.0)

        trozo = self.sr.AudioData(bytes(audio), self.micro.SAMPLE_RATE, self.micro.SAMPLE_WIDTH)

        try:
            return self.reconocedor.recognize_google(trozo, language=self.idioma)
        except self.sr.UnknownValueError:
            self._ajustar_umbral(1.1)  # no lo ha entendido: probablemente era ruido
            return None
        except self.sr.RequestError as error:
            raise RuntimeError(f"Google no responde: {error}") from error

    def _cerrar_interrupcion(self) -> None:
        """Cierra el stream temporal usado para detectar que nos interrumpes."""
        if self._interrupcion_fuente is not None:
            try:
                self.micro.__exit__(None, None, None)
            except Exception:
                pass
        self._interrupcion_fuente = None
        self._interrupcion_audio = bytearray()
        self._interrupcion_hablando = False
        self._interrupcion_silencio_desde = None

    def frase_inmediata(self) -> str | None:
        """Detecta una frase nueva mientras el agente está hablando.

        SpeechRecognition no ofrece texto parcial de Google en tiempo real, pero
        sí podemos leer bloques cortos del mismo micrófono mientras se reproduce
        la respuesta. Cuando detectamos voz y luego silencio, mandamos ese audio
        a Google y devolvemos la nueva frase para interrumpir al agente.
        """
        if self._interrupcion_fuente is None:
            try:
                # __enter__ deja el stream abierto entre llamadas a este método.
                self._interrupcion_fuente = self.micro.__enter__()
            except Exception as error:
                self._interrupcion_ultimo_error = str(error)
                self._cerrar_interrupcion()
                return None

        try:
            fuente = self._interrupcion_fuente
            datos = fuente.stream.read(self.micro.CHUNK, exception_on_overflow=False)
            energia = self._energia(datos) if datos else 0.0
            umbral = max(self.umbral, 180.0)
            hay_voz = energia >= umbral

            if not self._interrupcion_hablando:
                if not hay_voz:
                    return None
                self._interrupcion_hablando = True
                self._interrupcion_audio = bytearray(datos)
                self._interrupcion_silencio_desde = None
                return None

            self._interrupcion_audio += datos
            if hay_voz:
                self._interrupcion_silencio_desde = None
            elif self._interrupcion_silencio_desde is None:
                self._interrupcion_silencio_desde = time.time()

            if (
                self._interrupcion_silencio_desde is None
                or time.time() - self._interrupcion_silencio_desde < 0.42
            ):
                return None

            audio = self.sr.AudioData(
                bytes(self._interrupcion_audio), self.micro.SAMPLE_RATE, self.micro.SAMPLE_WIDTH
            )
            self._cerrar_interrupcion()
            try:
                return self.reconocedor.recognize_google(audio, language=self.idioma)
            except self.sr.UnknownValueError:
                return None
            except self.sr.RequestError as error:
                log(f"{GRIS}No se pudo transcribir la interrupción: {error}{RESET}")
                return None
        except Exception as error:
            self._interrupcion_ultimo_error = str(error)
            self._cerrar_interrupcion()
            return None

    def descartar(self) -> None:
        self.nivel = 0.0
        self.parcial = ""
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
               micro: int | None = None, idioma: str = "es-ES"):
    """Elige el oído disponible (por defecto, reconocimiento en español)."""
    if preferido in ("auto", "google"):
        try:
            return OidoGoogle(idioma=idioma, silencio_final=silencio, micro=micro)
        except Exception as error:
            if preferido == "google":
                raise
            log(f"{GRIS}[i] Sin SpeechRecognition ({type(error).__name__}), uso el de Windows{RESET}")

    if not SAPI_DISPONIBLE:
        raise RuntimeError("Falta pywin32: pip install pywin32")

    return OidoSAPI()


class Orbe:
    """Ventanita flotante con un orbe que muestra el estado de la conversación."""

    ANCHO, ALTO = 300, 180
    CLAVE = "#ff00fe"  # color "trampa" que Tk vuelve transparente

    ESTADOS = {
        "inactivo": ("#6b7480", "en espera"),
        "pausado": ("#6b7480", "pausado - clic para activar"),
        "escuchando": ("#2f9ff5", "escuchando"),
        "pensando": ("#f5a623", "pensando"),
        "hablando": ("#2ecc71", "hablando"),
        "error": ("#e5484d", "error"),
    }

    def __init__(self, al_click, al_salir, titulo: str = "Voz", al_elegir_micro=None):
        self.al_click = al_click
        self.al_salir = al_salir
        self.al_elegir_micro = al_elegir_micro
        self.cola: queue.Queue[tuple[str, str]] = queue.Queue()

        self.root = tk.Tk()
        self.root.title(titulo)
        self.root.overrideredirect(True)                     # sin bordes ni barra
        self.root.attributes("-topmost", True)               # siempre por encima
        self.root.attributes("-transparentcolor", self.CLAVE)
        self.root.attributes("-alpha", 0.95)
        self.root.configure(bg=self.CLAVE)

        x = (self.root.winfo_screenwidth() - self.ANCHO) // 2
        self.root.geometry(f"{self.ANCHO}x{self.ALTO}+{x}+10")

        self.lienzo = tk.Canvas(
            self.root, width=self.ANCHO, height=self.ALTO,
            bg=self.CLAVE, highlightthickness=0,
        )
        self.lienzo.pack()

        # Centro y radios del orbe
        self.cx, self.cy, self.radio = self.ANCHO // 2, 74, 40

        self.halo = self.lienzo.create_oval(0, 0, 0, 0, outline="")
        self.anillo = self.lienzo.create_oval(0, 0, 0, 0, outline="")
        self.nucleo = self.lienzo.create_oval(0, 0, 0, 0, outline="")
        self.brillo = self.lienzo.create_oval(0, 0, 0, 0, outline="")
        self.arco = self.lienzo.create_arc(0, 0, 0, 0, style="arc", width=3, outline="")
        self.item_etiqueta = self.lienzo.create_text(
            self.cx, 140, text="", fill="#ffffff", font=("Segoe UI", 11, "bold"),
        )
        self.item_detalle = self.lienzo.create_text(
            self.cx, 162, text="", fill="#c9d1d9", font=("Segoe UI", 8), width=280,
        )

        self.estado_actual = "inactivo"
        self.fase = 0.0
        self.pulso = 0.0
        self.texto_detalle = ""
        self.nivel_objetivo = 0.0   # lo que oye ahora mismo el micrófono
        self.nivel_visible = 0.0    # suavizado, para que no dé tirones
        self._arrastre = None

        # Arrastrar el orbe y distinguirlo de un clic
        self.lienzo.bind("<Button-1>", self._pulsar)
        self.lienzo.bind("<B1-Motion>", self._arrastrar)
        self.lienzo.bind("<ButtonRelease-1>", self._soltar)
        self.lienzo.bind("<Button-3>", self._menu)

        self._menu_emergente = tk.Menu(self.root, tearoff=0)
        self._menu_emergente.add_command(label="Pausar / reanudar", command=self.al_click)
        self._menu_emergente.add_command(
            label="Elegir micrófono…",
            command=lambda: self.al_elegir_micro and self.al_elegir_micro(),
        )
        self._menu_emergente.add_separator()
        self._menu_emergente.add_command(label="Salir", command=self.al_salir)

        self.root.after(40, self._tick)

    # --- cosas que llaman otros hilos (van por cola, Tk no es thread-safe) ---

    def estado(self, nombre: str) -> None:
        self.cola.put(("estado", nombre))

    def detalle(self, texto: str) -> None:
        self.cola.put(("detalle", texto))

    def progreso(self, texto: str, nivel: float) -> None:
        """Lo que está oyendo en este instante: texto parcial y nivel de voz."""
        self.cola.put(("progreso", texto, nivel))

    # --- interacción ---

    def _pulsar(self, evento) -> None:
        self._arrastre = (evento.x_root, evento.y_root, self.root.winfo_x(), self.root.winfo_y())

    def _arrastrar(self, evento) -> None:
        if not self._arrastre:
            return
        x0, y0, vx, vy = self._arrastre
        self.root.geometry(f"+{vx + evento.x_root - x0}+{vy + evento.y_root - y0}")

    def _soltar(self, evento) -> None:
        if not self._arrastre:
            return
        x0, y0, _, _ = self._arrastre
        # Si no se movió apenas, era un clic: pausar o reanudar
        if abs(evento.x_root - x0) < 4 and abs(evento.y_root - y0) < 4:
            self.al_click()
        self._arrastre = None

    def _menu(self, evento) -> None:
        try:
            self._menu_emergente.tk_popup(evento.x_root, evento.y_root)
        finally:
            self._menu_emergente.grab_release()

    # --- dibujo ---

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
                elif tipo == "progreso":
                    self.texto_detalle = valor
                    self.nivel_objetivo = mensaje[2]
                else:
                    self.texto_detalle = valor
        except queue.Empty:
            pass

        # El nivel sube rápido y baja despacio, como un vúmetro de verdad
        destino = self.nivel_objetivo
        paso = 0.5 if destino > self.nivel_visible else 0.15
        self.nivel_visible += (destino - self.nivel_visible) * paso

        color, etiqueta = self.ESTADOS.get(self.estado_actual, self.ESTADOS["inactivo"])
        self.fase += 0.22

        # Cada estado se mueve distinto: en espera quieto, al hablar late fuerte
        if self.estado_actual == "hablando":
            latido = 1 + 0.11 * math.sin(self.fase * 1.6)
        elif self.estado_actual == "escuchando":
            latido = 1 + 0.05 * math.sin(self.fase * 0.9)
        elif self.estado_actual == "pensando":
            latido = 1 + 0.03 * math.sin(self.fase * 0.6)
        else:
            latido = 1.0

        # Y si hay voz, el orbe crece con ella: feedback de que te está oyendo
        radio = self.radio * latido * (1 + 0.28 * self.nivel_visible)
        if self.estado_actual == "escuchando":
            color = self._aclarar(color, 0.35 * self.nivel_visible)

        def caja(extra: float):
            return (
                self.cx - radio - extra, self.cy - radio - extra,
                self.cx + radio + extra, self.cy + radio + extra,
            )

        self.lienzo.coords(self.halo, *caja(16))
        self.lienzo.itemconfig(self.halo, fill=self._oscurecer(color, 0.28))

        self.lienzo.coords(self.anillo, *caja(7))
        self.lienzo.itemconfig(self.anillo, fill=self._oscurecer(color, 0.55))

        self.lienzo.coords(self.nucleo, *caja(0))
        self.lienzo.itemconfig(self.nucleo, fill=color)

        # Reflejo blanco arriba a la izquierda: le da aspecto de bola de cristal
        brillo = self.radio * 0.42
        desvio = self.radio * 0.28
        self.lienzo.coords(
            self.brillo,
            self.cx - brillo - desvio, self.cy - brillo - desvio,
            self.cx + brillo - desvio, self.cy + brillo - desvio,
        )

        # El arco gira cuando piensa o escucha; escuchando, además, marca el nivel
        girando = self.estado_actual in ("pensando", "escuchando")
        inicio = (self.fase * (60 if self.estado_actual == "pensando" else 25)) % 360
        self.lienzo.coords(self.arco, *caja(13))
        self.lienzo.itemconfig(
            self.arco,
            start=inicio if girando else 90,
            extent=60 + 300 * self.nivel_visible if self.estado_actual == "escuchando" else 100,
            outline="#ffffff" if girando else self._oscurecer(color, 0.4),
        )

        # Mientras escucha, la línea de abajo enseña lo que va entendiendo
        if self.estado_actual == "escuchando":
            if self.texto_detalle:
                detalle = f"« {self.texto_detalle} »"
            elif self.nivel_visible > 0.18:
                detalle = "te oigo…"
            else:
                detalle = "habla cuando quieras"
        else:
            detalle = self.texto_detalle

        self.lienzo.itemconfig(self.item_etiqueta, text=etiqueta)
        self.lienzo.itemconfig(self.item_detalle, text=detalle)

        self.root.after(40, self._tick)

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

        self.al_elegir(self.indice)
        self.cerrar()

    def _refrescar(self) -> None:
        """Dibuja la barra de nivel y los errores del dispositivo."""
        if not self.ventana.winfo_exists():
            return

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
        if self.medidor:
            self.medidor.cerrar()
        self.ventana.destroy()
        self.al_cerrar()


def bucle_voz(chat: ChatCLI, oido, voz, orbe: Orbe, parar: threading.Event,
              pausa: threading.Event, mudo: bool = False) -> None:
    """Bucle principal: escucha, pregunta al modelo y responde en voz alta."""
    pendiente = None
    ultimo_aviso = {"texto": "", "cuando": 0.0}

    def mostrar_progreso(fase: str, texto: str, nivel: float) -> None:
        """Retroalimentación en vivo: el orbe late con tu voz y enseña lo que oye."""
        if fase == "transcribiendo":
            orbe.estado("pensando")
            orbe.detalle("entendiendo lo que has dicho…")
            return

        ahora = time.time()
        # El audio llega cada 30 ms: redibujamos solo cuando cambia algo
        if texto == ultimo_aviso["texto"] and ahora - ultimo_aviso["cuando"] < 0.1:
            return

        ultimo_aviso["texto"], ultimo_aviso["cuando"] = texto, ahora
        orbe.progreso(texto, nivel)

    try:
        while not parar.is_set():
            if pausa.is_set():
                oido.descartar()
                orbe.estado("pausado")
                orbe.detalle("")
                time.sleep(0.2)
                continue

            orbe.estado("escuchando")
            orbe.detalle("")
            ultimo_aviso["texto"] = ""

            texto = pendiente or oido.escuchar(parar, al_progreso=mostrar_progreso)
            pendiente = None
            orbe.progreso("", 0.0)

            if parar.is_set():
                break
            if not texto:
                # No ha entendido nada: seguimos escuchando sin molestar
                continue

            log(f"\n{AZUL}tú> {texto}{RESET}")
            orbe.estado("pensando")
            orbe.detalle(f"tú: {texto}")

            metricas = chat.preguntar(texto, imprimir=False)

            if metricas.error:
                log(f"{ROJO}Error: {metricas.error}{RESET}")
                orbe.estado("error")
                detalle_error = metricas.error.splitlines()[0][:120]
                orbe.detalle(detalle_error)
                if not mudo:
                    if "401" in metricas.error or "API_CHATGPT" in metricas.error:
                        voz.decir("La clave de la API no es válida o está desactivada. Revisa el archivo punto env.")
                    else:
                        voz.decir("Ha habido un error al consultar el modelo. Mira el detalle en pantalla.")
                continue

            log(f"{metricas.texto}")
            log(f"{GRIS}[{metricas.resumen_corto()}]{RESET}")

            respuesta = limpiar_para_voz(metricas.texto)
            orbe.estado("hablando")
            orbe.detalle(respuesta[:70])

            if not mudo:
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
        orbe.detalle(f"{type(error).__name__}: {error}"[:80])
    finally:
        parar.set()


def generar_icono(destino: Path, centro: tuple = (127, 212, 255),
                  borde: tuple = (18, 70, 168)) -> Path:
    """Escribe un .ico con el orbe dibujado a mano (sin dependencias)."""
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
    descripcion: str = "Hablar con el modelo (orbe flotante)",
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


def crear_parser(descripcion: str = "Habla con el modelo usando un orbe flotante.") -> argparse.ArgumentParser:
    """Los flags del modo voz, que también usa quien reutilice el orbe."""
    parser = argparse.ArgumentParser(description=descripcion)
    parser.add_argument("-m", "--modelo", default=MODELO_DEFECTO, help="Modelo a usar")
    parser.add_argument("--oido", choices=["auto", "sapi", "google", "teclado"], default="auto",
                        help="De dónde viene tu voz (por defecto, reconocimiento en español)")
    parser.add_argument("--idioma", default="es-ES",
                        help="Idioma del reconocimiento de voz (por defecto: es-ES)")
    parser.add_argument("--silencio", type=float, default=0.7,
                        help="Segundos de silencio para dar por terminada tu frase (0.7)")
    parser.add_argument("--texto", action="store_true", help="Escribir en consola en vez de hablar")
    parser.add_argument("--voz-salida", default="David", help="Nombre de la voz SAPI o ID de voz Fish Audio")
    parser.add_argument("--voz-motor", choices=["sapi", "fish"], default=None,
                        help="Motor de salida: sapi (Windows) o fish (Fish Audio)")
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

    return parser


def main() -> None:
    args = crear_parser().parse_args()

    configurar_salida()

    # Micrófono: lo que diga el comando, o el que guardaste la última vez
    micro_pedido = args.micro if args.micro is not None else leer_config().get("micro")
    indice_micro = resolver_micro(micro_pedido)

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


def arrancar(chat, args, indice_micro: int | None = None) -> None:
    """Monta el orbe, los oídos y la voz alrededor del cerebro que se le pase.

    Basta con que `chat` tenga `modelo` y `preguntar(texto, imprimir=...)`, así
    que sirve igual el chat de consola que el agente con memoria.
    """
    # El oído se crea dentro del hilo, así que el menú lo mira de aquí
    caja: dict = {"oido": None}

    parar = threading.Event()
    pausa = threading.Event()

    def alternar_pausa() -> None:
        if pausa.is_set():
            pausa.clear()
        else:
            pausa.set()

    def salir() -> None:
        parar.set()
        orbe.cerrar()

    def elegir_micro() -> None:
        """Abre el selector de entrada de voz (el orbe sigue en el hilo de Tk)."""
        oido = caja["oido"]
        if oido is None:
            orbe.detalle("el oído aún se está preparando…")
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

        SelectorMicro(
            orbe.root, al_elegir, al_cerrar,
            indice_actual=getattr(oido, "indice_micro", None),
        )

    orbe = Orbe(al_click=alternar_pausa, al_salir=salir, al_elegir_micro=elegir_micro)
    orbe.estado("inactivo")

    def hilo_voz() -> None:
        """Todo el trabajo pesado va aquí; el orbe vive en el hilo principal."""
        try:
            oido = OidoTeclado() if args.texto else crear_oido(
                args.oido, args.silencio, indice_micro, args.idioma
            )
            caja["oido"] = oido
            if args.mudo:
                voz = None
            elif (args.voz_motor or os.getenv("VOZ_MOTOR", "sapi")).lower() == "fish":
                voz = VozFishAudio(args.voz_salida if args.voz_salida != "David" else None,
                                   args.velocidad, args.volumen)
            else:
                voz = Voz(args.voz_salida, args.velocidad, args.volumen)

            log(f"{AZUL}Modo voz listo.{RESET} Modelo: {chat.modelo}")
            log(f"{GRIS}Oído: {oido.descripcion}" + (f" | Voz: {voz.nombre}" if voz else " | mudo"))
            log(f"El orbe late con tu voz y verás lo que entiende mientras hablas.")
            log(f"Se calla {args.silencio:g} s tras tu última palabra y entonces te responde.")
            log(f"Clic en el orbe para pausar; clic derecho para salir.{RESET}")

            bucle_voz(chat, oido, voz, orbe, parar, pausa, mudo=args.mudo)
        except Exception as error:
            log(f"{ROJO}Error al arrancar el modo voz: {type(error).__name__}: {error}{RESET}")
            orbe.estado("error")
            orbe.detalle(f"{type(error).__name__}: {error}"[:80])
        finally:
            parar.set()

    threading.Thread(target=hilo_voz, daemon=True).start()

    try:
        orbe.run()
    except KeyboardInterrupt:
        pass
    finally:
        parar.set()
        cerrar = getattr(chat, "cerrar_sesion", None)
        if cerrar:
            cerrar()


if __name__ == "__main__":
    main()
