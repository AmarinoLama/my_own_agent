"""Pruebas sin dispositivos físicos del filtrado de interrupciones de voz."""

import collections
import queue
import struct
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import voz
from voz import DetectorVoz, FraseVoz, OidoGoogle, Orbe

try:
    from voz import DetectorPalmadas
except ImportError:
    DetectorPalmadas = None

try:
    from voz import _ReproductorMCI
except ImportError:
    _ReproductorMCI = None


class DetectorVozTests(unittest.TestCase):
    def test_remuestrea_48khz_a_16khz(self):
        datos = struct.pack("<960h", *range(960))

        remuestreado = DetectorVoz.a_16k(datos, 48000)

        self.assertEqual(320 * 2, len(remuestreado))

    def test_fallback_por_nivel_descarta_silencio_y_acepta_voz(self):
        detector = DetectorVoz.__new__(DetectorVoz)
        detector._vad = None
        silencio = bytes(DetectorVoz.BYTES_MARCO)
        voz = struct.pack("<320h", *([1000, -1000] * 160))

        self.assertFalse(detector.es_voz(silencio))
        self.assertTrue(detector.es_voz(voz))

    def test_ignora_ruido_aislado_que_suena_a_voz(self):
        historial = collections.deque(
            [(bytes(DetectorVoz.BYTES_MARCO), estado) for estado in (False, True, False, False, False, False)],
            maxlen=8,
        )

        self.assertFalse(DetectorVoz.voz_sostenida(historial))

    def test_acepta_voz_sostenida_en_la_ventana(self):
        historial = collections.deque(
            [(bytes(DetectorVoz.BYTES_MARCO), estado) for estado in (True, True, True, True, True, False)],
            maxlen=8,
        )

        self.assertTrue(DetectorVoz.voz_sostenida(historial))

    def test_usa_clasificador_vad_en_vez_de_solo_energia(self):
        detector = DetectorVoz.__new__(DetectorVoz)
        detector._vad = SimpleNamespace(is_speech=lambda _marco, _frecuencia: False)
        ruido_fuerte = struct.pack("<320h", *([5000, -5000] * 160))

        self.assertFalse(detector.es_voz(ruido_fuerte))

    def test_lee_marcos_por_muestras_y_no_por_bytes(self):
        self.assertEqual(320, DetectorVoz.muestras_marco(16000))
        self.assertEqual(960, DetectorVoz.muestras_marco(48000))
        self.assertEqual(1920, DetectorVoz.bytes_marco(48000))


@unittest.skipIf(DetectorPalmadas is None, "DetectorPalmadas no existe todavía en voz.py")
class DetectorPalmadasTests(unittest.TestCase):
    def setUp(self):
        self.detector = DetectorPalmadas(umbral_pico=3000)
        self.frecuencia = 48000
        self.silencio = bytes(960)
        self.palmada = struct.pack("<480h", 12000, *([0] * 479))

    def _silencio(self, inicio):
        for n in range(5):
            self.detector.alimentar(self.silencio, self.frecuencia, ahora=inicio + n * 0.01)

    def test_abre_solo_tras_dos_palmadas_en_intervalo(self):
        self.assertFalse(self.detector.alimentar(self.palmada, self.frecuencia, ahora=1.0))
        self._silencio(1.1)
        self.assertTrue(self.detector.alimentar(self.palmada, self.frecuencia, ahora=1.55))

    def test_ruido_y_voz_menos_impulsiva_no_activan(self):
        voz = struct.pack("<480h", *([2500] * 480))
        ruido_bajo = struct.pack("<480h", *([1200, -1200] * 240))

        self.assertFalse(self.detector.alimentar(voz, self.frecuencia, ahora=1.0))
        self.assertFalse(self.detector.alimentar(ruido_bajo, self.frecuencia, ahora=1.5))

    def test_rechaza_palmadas_demasiado_proximas_y_rearma(self):
        self.detector.alimentar(self.palmada, self.frecuencia, ahora=1.0)
        self._silencio(1.1)
        self.assertFalse(self.detector.alimentar(self.palmada, self.frecuencia, ahora=1.15))
        self._silencio(1.25)
        self.assertTrue(self.detector.alimentar(self.palmada, self.frecuencia, ahora=1.7))

    def test_un_impulso_no_repite_activacion(self):
        self.detector.alimentar(self.palmada, self.frecuencia, ahora=1.0)
        self._silencio(1.1)
        self.assertFalse(self.detector.alimentar(self.silencio, self.frecuencia, ahora=1.2))
        self.assertFalse(self.detector.alimentar(self.silencio, self.frecuencia, ahora=1.3))

    def test_el_candidato_expirado_necesita_otra_pareja(self):
        self.detector.alimentar(self.palmada, self.frecuencia, ahora=1.0)
        self._silencio(1.1)
        self.assertFalse(self.detector.alimentar(self.palmada, self.frecuencia, ahora=2.5))
        self._silencio(2.6)
        self.assertTrue(self.detector.alimentar(self.palmada, self.frecuencia, ahora=3.0))


class SegmentacionFrasesTests(unittest.TestCase):
    def test_ruido_aislado_no_abre_una_frase(self):
        segmentador = FraseVoz(16000, silencio=0.1, preroll=0.04)
        marco = bytes(640)

        self.assertIsNone(segmentador.alimentar(marco, True))
        self.assertIsNone(segmentador.alimentar(marco, False))
        self.assertIsNone(segmentador.alimentar(marco, False))
        self.assertFalse(segmentador.escuchando)

    def test_cierra_frase_tras_silencio_y_preserva_audio(self):
        segmentador = FraseVoz(16000, silencio=0.04, preroll=0.04, voz_minima=0.06)
        voz = b"\x01\x00" * 320
        silencio = bytes(640)

        for _ in range(6):
            self.assertIsNone(segmentador.alimentar(voz, True))
        self.assertTrue(segmentador.escuchando)
        self.assertIsNone(segmentador.alimentar(silencio, False))
        frase = segmentador.alimentar(silencio, False)

        self.assertIsNotNone(frase)
        self.assertIn(voz, frase)
        self.assertFalse(segmentador.escuchando)

    def test_limita_audio_largo_sin_quedarse_atrapado_en_escucha(self):
        segmentador = FraseVoz(16000, silencio=0.5, duracion_maxima=0.16, voz_minima=0.06)
        voz = b"\x01\x00" * 320

        for _ in range(5):
            segmentador.alimentar(voz, True)
        frase = None
        for _ in range(5):
            frase = segmentador.alimentar(voz, True) or frase

        self.assertIsNotNone(frase)
        self.assertFalse(segmentador.escuchando)


@unittest.skipIf(_ReproductorMCI is None, "_ReproductorMCI no existe todavía en voz.py")
class ReproductorMCITests(unittest.TestCase):
    def test_reproduce_consulta_estado_y_detiene_con_alias(self):
        comandos = []

        def enviar(comando, respuesta=False):
            comandos.append(comando)
            return "playing" if respuesta else ""

        reproductor = _ReproductorMCI(enviar_comando=enviar, ruta_corta=lambda ruta: ruta)
        reproductor.reproducir("C:/temp/voz.mp3", 0.75)

        self.assertTrue(reproductor.hablando())
        reproductor.detener()

        self.assertIn('open "C:/temp/voz.mp3" type MPEGVideo alias AgenteVoz', comandos[0])
        self.assertIn("setaudio AgenteVoz", comandos[1])
        self.assertIn("play AgenteVoz", comandos[2])
        self.assertIn("stop AgenteVoz", comandos[-2])
        self.assertIn("close AgenteVoz", comandos[-1])


class OrbeTransparenciaTests(unittest.TestCase):
    def _imagen_renderizada(self, estado):
        class LienzoFalso:
            def itemconfig(self, _item, **kwargs):
                self.imagen = kwargs["image"]

        orbe = Orbe.__new__(Orbe)
        orbe.estado_actual = estado
        orbe.tiempo = 0.0
        orbe.nivel_visible = 0.0
        orbe.lienzo = LienzoFalso()
        orbe.sprite = 1
        capturada = {}

        def foto_falsa(data):
            capturada["ppm"] = data
            return object()

        with patch("voz.tk.PhotoImage", side_effect=foto_falsa):
            orbe._pintar()

        _, ancho, alto, _, pixels = capturada["ppm"].split(b" ", 4)
        return int(ancho), int(alto), pixels

    def test_cinco_estados_pintan_la_pastilla_y_los_puntos(self):
        if voz.Image is None:
            self.skipTest("Pillow no está instalado")

        clave = bytes(Orbe.CLAVE_RGB)
        negro = bytes(Orbe.NEGRO)
        for estado in ("inactivo", "escuchando", "pensando", "hablando", "error"):
            with self.subTest(estado=estado):
                ancho, _, pixels = self._imagen_renderizada(estado)
                # Un punto seguro dentro de la pastilla sigue en negro
                dentro = (20 * ancho + 40) * 3
                self.assertEqual(negro, pixels[dentro:dentro + 3])
                pintados = sum(
                    1 for inicio in range(0, len(pixels), 3)
                    if pixels[inicio:inicio + 3] != clave
                )
                self.assertGreater(pintados, 5000)
                # y los 5 puntos se pintan encima, dentro de la pastilla
                puntos = sum(
                    1 for inicio in range(0, len(pixels), 3)
                    if pixels[inicio:inicio + 3] not in (clave, negro)
                )
                self.assertGreater(puntos, 100)

    def test_los_puntos_de_hablando_estan_alineados_y_naranjas(self):
        if voz.Image is None:
            self.skipTest("Pillow no está instalado")

        ancho, _, pixels = self._imagen_renderizada("hablando")
        centro_x, centro_y = Orbe.ANCHO // 2, Orbe.ALTO_PASTILLA // 2
        naranjas = []
        for inicio in range(0, len(pixels), 3):
            rojo, azul = pixels[inicio], pixels[inicio + 2]
            if rojo > 140 and rojo > azul + 60:
                posicion = inicio // 3
                naranjas.append((posicion % ancho, posicion // ancho))

        self.assertTrue(naranjas)
        # En línea horizontal y en el centro de la pastilla, ni se salen
        self.assertTrue(all(abs(y - centro_y) < 16 for _, y in naranjas))
        self.assertTrue(all(abs(x - centro_x) < 60 for x, _ in naranjas))


class TranscripcionInterrupcionTests(unittest.TestCase):
    def setUp(self):
        self.oido = OidoGoogle.__new__(OidoGoogle)
        self.oido.sr = SimpleNamespace(AudioData=lambda datos, frecuencia, ancho: (datos, frecuencia, ancho))
        self.oido.idioma = "es-ES"
        self.oido.micro = SimpleNamespace(SAMPLE_RATE=16000, SAMPLE_WIDTH=2)
        self.oido.perfil_voz = None

    def test_solo_acepta_transcripcion_con_confianza_suficiente(self):
        self.oido.reconocedor = SimpleNamespace(recognize_google=Mock(return_value={
            "alternative": [
                {"transcript": "ruido interpretado", "confidence": 0.32},
                {"transcript": "para un momento", "confidence": 0.91},
            ],
        }))

        resultado = self.oido._transcribir_interrupcion(bytes(640))

        self.assertEqual("para un momento", resultado)

    def test_acepta_formato_real_de_speech_recognition_sin_confidence(self):
        self.oido.reconocedor = SimpleNamespace(recognize_google=Mock(return_value={
            "result": [{"alternative": [{"transcript": "para un momento"}]}],
        }))

        self.assertEqual("para un momento", self.oido._transcribir_interrupcion(bytes(640)))

    def test_descarta_confianza_explicita_demasiado_baja(self):
        self.oido.reconocedor = SimpleNamespace(recognize_google=Mock(return_value={
            "result": [{"alternative": [{"transcript": "ruido", "confidence": 0.2}]}],
        }))

        self.assertIsNone(self.oido._transcribir_interrupcion(bytes(640)))

    def test_descarta_audio_que_google_no_puede_transcribir(self):
        error = type("UnknownValueError", (Exception,), {})
        self.oido.sr.UnknownValueError = error
        self.oido.reconocedor = SimpleNamespace(recognize_google=Mock(side_effect=error()))

        self.assertIsNone(self.oido._transcribir_interrupcion(bytes(640)))


class OrdenDeCierreTests(unittest.TestCase):
    def test_cierrate_jarvis_cierra(self):
        self.assertTrue(voz.es_orden_de_cierre("cierrate jarvis"))

    def test_tildes_y_mayusculas_no_importan(self):
        self.assertTrue(voz.es_orden_de_cierre("Ciérrate, Jarvis"))

    def test_hasta_luego_tambien_cierra(self):
        self.assertTrue(voz.es_orden_de_cierre("hasta luego jarvis"))

    def test_muletillas_de_cortesia_no_estorban(self):
        self.assertTrue(voz.es_orden_de_cierre("por favor cierrate ya jarvis"))

    def test_sin_el_nombre_no_cierra(self):
        self.assertFalse(voz.es_orden_de_cierre("ciérrate"))

    def test_hablando_de_otra_cosa_no_cierra(self):
        self.assertFalse(voz.es_orden_de_cierre("cierra la puerta jarvis"))
        self.assertFalse(voz.es_orden_de_cierre("jarvis abre el calendario"))

    def test_frase_vacia_no_cierra(self):
        self.assertFalse(voz.es_orden_de_cierre(""))

    def test_la_orden_viaja_por_la_cola_de_tk(self):
        orbe = Orbe.__new__(Orbe)
        orbe.cola = queue.Queue()
        orbe.pedir_cierre()

        self.assertEqual(("salir", None), orbe.cola.get_nowait())

    def test_el_tick_de_la_animacion_es_el_que_cierra(self):
        orbe = Orbe.__new__(Orbe)
        orbe.cola = queue.Queue()
        orbe.cola.put(("salir", None))
        llamadas = []
        orbe.al_salir = lambda: llamadas.append("salir")

        orbe._tick()      # sin el resto de atributos: ni pinta ni reprograma

        self.assertEqual(["salir"], llamadas)


class DespedidaTests(unittest.TestCase):
    def _cerrar_con_la_orden(self, voz_falsa, mudo=False, ordenes=None):
        ordenes = ordenes if ordenes is not None else []

        class OidoFalso:
            def escuchar(self, parar, al_progreso=None):
                return "ciérrate jarvis"

            def descartar(self):
                pass

        parar = threading.Event()
        voz.bucle_voz(
            chat=SimpleNamespace(),
            oido=OidoFalso(),
            voz=voz_falsa,
            orbe=SimpleNamespace(estado=lambda *_: None, progreso=lambda *_: None),
            parar=parar,
            pausa=threading.Event(),
            mudo=mudo,
            al_cerrar=lambda: ordenes.append("cerrar"),
        )
        return ordenes, parar

    def test_termina_de_despedirse_antes_de_cerrar(self):
        ordenes = []

        class VozFalsa:
            def decir(self, texto):
                ordenes.append(f"decir:{texto}")

            def esperar(self):
                ordenes.append("esperar")

        ordenes, parar = self._cerrar_con_la_orden(VozFalsa(), ordenes=ordenes)

        self.assertEqual(["decir:Hasta luego.", "esperar", "cerrar"], ordenes)
        self.assertTrue(parar.is_set())

    def test_si_falla_la_despedida_igual_se_cierra(self):
        class VozRota:
            def decir(self, texto):
                raise RuntimeError("sin audio")

            def esperar(self):
                raise AssertionError("no debe esperar si ni siquiera habló")

        ordenes, _ = self._cerrar_con_la_orden(VozRota())

        self.assertEqual(["cerrar"], ordenes)

    def test_en_mudo_no_habla_y_aun_asi_cierra(self):
        ordenes, _ = self._cerrar_con_la_orden(None, mudo=True)

        self.assertEqual(["cerrar"], ordenes)


class CuotaAgotadaTests(unittest.TestCase):
    def _hablar_con_error(self, error):
        dichas = []
        parar = threading.Event()

        class ChatFalso:
            def preguntar(self, texto, imprimir=False):
                parar.set()                 # un solo turno: así el bucle acaba
                return SimpleNamespace(error=error, texto="",
                                       resumen_corto=lambda: "")

        class OidoFalso:
            def escuchar(self, parar, al_progreso=None):
                return "hola jarvis"

            def descartar(self):
                pass

        voz.bucle_voz(
            chat=ChatFalso(),
            oido=OidoFalso(),
            voz=SimpleNamespace(decir=lambda texto: dichas.append(texto),
                                esperar=lambda: None),
            orbe=SimpleNamespace(estado=lambda *_: None, progreso=lambda *_: None),
            parar=parar,
            pausa=threading.Event(),
            mudo=False,
            al_cerrar=None,
        )
        return dichas

    def test_la_cuota_agotada_se_explica_en_voz(self):
        dichas = self._hablar_con_error(
            "Error code: 429 - rate_limit_error: You've reached today's "
            "free-model token quota."
        )

        self.assertEqual(["Se ha agotado la cuota gratuita por hoy. "
                          "Vuelve a probar más tarde."], dichas)

    def test_un_error_cualquiera_sigue_siendo_el_de_siempre(self):
        dichas = self._hablar_con_error("Error code: 500 - server error")

        self.assertEqual(["Ha habido un error al consultar el modelo. "
                          "Mira la consola."], dichas)


if __name__ == "__main__":
    unittest.main()
