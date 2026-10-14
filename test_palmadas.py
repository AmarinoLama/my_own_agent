"""Pruebas del detector de dos palmadas que abren el agente."""
import os
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import voz
from voz import (MICRO_MUERTO, Palmadas, ReposoPalmadas, _pico_de_fondo,
                 desinstalar_palmadas, elegir_micro_palmadas, instalar_palmadas,
                 pico_sano, umbral_palmadas)

BLOQUE = 320  # 20 ms a 16 kHz, el trozo que lee el oído


def trozo(nivel: float) -> bytes:
    """Un trozo de 20 ms cuyo pico vale `nivel` (lo que mide el detector)."""
    valores = [int(nivel) if i % 2 == 0 else -int(nivel) for i in range(BLOQUE)]
    return struct.pack(f"<{BLOQUE}h", *valores)


def ruido(desde: float, hasta: float, nivel: float = 60.0) -> list[tuple[float, float]]:
    """Trozos flojos (silencio de habitación) entre dos instantes."""
    return [(nivel, round(desde + i * 0.02, 3))
            for i in range(max(0, int((hasta - desde) / 0.02)))]


def palmada(cuando: float, nivel: float = 20000.0) -> list[tuple[float, float]]:
    """Una palmada: el golpe y los 60 ms de flojera que lo cierran."""
    return [(nivel, round(cuando, 3)),
            (60.0, round(cuando + 0.02, 3)),
            (60.0, round(cuando + 0.04, 3)),
            (60.0, round(cuando + 0.06, 3))]


def oir(secuencia: list[tuple[float, float]]) -> list[bool]:
    """Alimenta el detector con (pico, instante) y devuelve si encaja cada trozo."""
    detector = Palmadas()
    return [detector.alimentar(trozo(pico), ahora=instante)
            for pico, instante in secuencia]


class PalmadasTest(unittest.TestCase):
    def test_das_dos_palmadas_y_se_abre(self):
        secuencia = (ruido(0.0, 0.20) + palmada(0.20)
                     + ruido(0.30, 0.90) + palmada(0.90))
        self.assertTrue(any(oir(secuencia)))

    def test_la_cola_de_la_palmada_no_la_hace_parecer_voz(self):
        # Una palmada de verdad (grabada con el MHW-100) tiene una cola de
        # reverberación que tarda 140 ms en bajar de la mitad del pico: con el
        # «flojo» medido contra el umbral global duraba más de 100 ms y el
        # detector la descartaba como si fuera una sílaba.
        def con_cola(cuando):
            niveles = [20000, 9000, 6000, 4000, 3000, 2200, 1600, 1100, 700, 400]
            return [(float(nivel), round(cuando + i * 0.02, 3))
                    for i, nivel in enumerate(niveles)]

        secuencia = (ruido(0.0, 0.20) + con_cola(0.20)
                     + ruido(0.44, 0.90) + con_cola(0.90))
        self.assertTrue(any(oir(secuencia)))

    def test_una_palmada_sola_no_basta(self):
        secuencia = ruido(0.0, 0.20) + palmada(0.20) + ruido(0.30, 3.0)
        self.assertFalse(any(oir(secuencia)))

    def test_la_voz_no_se_confunde_con_palmadas(self):
        secuencia = ruido(0.0, 0.20)
        for palabra in (0.20, 1.00):
            # 300 ms de voz seguida, como una sílaba larga
            secuencia += [(9000.0, round(palabra + i * 0.02, 3)) for i in range(15)]
            secuencia += ruido(palabra + 0.30, palabra + 0.50)
        self.assertFalse(any(oir(secuencia)))

    def test_palmadas_muy_pegadas_cuentan_como_una(self):
        # 40 ms de distancia: es el mismo golpe partido en dos
        secuencia = (ruido(0.0, 0.20) + palmada(0.20)
                     + ruido(0.28, 0.30) + palmada(0.30))
        self.assertFalse(any(oir(secuencia)))

    def test_palmadas_muy_separadas_empiezan_de_cero(self):
        # La primera y la segunda no valen (2,3 s); la tercera sí, porque
        # entonces la segunda pasa a ser la primera de la pareja.
        secuencia = (ruido(0.0, 0.20) + palmada(0.20)
                     + ruido(0.30, 2.50) + palmada(2.50)
                     + ruido(2.60, 2.90) + palmada(2.90))
        respuestas = oir(secuencia)
        self.assertFalse(any(respuestas[:-1]))
        self.assertTrue(respuestas[-1])

    def test_el_ruido_de_la_habitacion_no_dispara(self):
        secuencia = [(800.0, round(i * 0.02, 3)) for i in range(250)]  # 5 s
        self.assertFalse(any(oir(secuencia)))

    def test_reiniciar_hace_que_la_siguiente_palmada_sea_la_primera(self):
        detector = Palmadas()
        for pico, instante in palmada(0.20):
            self.assertFalse(detector.alimentar(trozo(pico), ahora=instante))
        detector.reiniciar()
        respuestas = [detector.alimentar(trozo(pico), ahora=0.60 + t)
                      for pico, t in palmada(0.0)]
        self.assertFalse(any(respuestas))

    def test_con_umbral_fijo_los_golpes_debiles_no_cuentan(self):
        # AGENTE_PALMADA_UMBRAL fija cuánto ha de subir el pico
        debil = palmada(0.20, nivel=1000.0) + ruido(0.30, 0.90) + palmada(0.90, nivel=1000.0)
        fuerte = palmada(0.20, nivel=9000.0) + ruido(0.30, 0.90) + palmada(0.90, nivel=9000.0)

        def oir_con(umbral, secuencia):
            detector = Palmadas(umbral)
            return any(detector.alimentar(trozo(pico), ahora=instante)
                       for pico, instante in secuencia)

        self.assertFalse(oir_con(3500.0, debil))   # 1000 no pasa de 3500
        self.assertTrue(oir_con(3500.0, fuerte))   # 9000 sí pasa
        self.assertFalse(oir_con(None, debil))     # por defecto el mínimo es 8000
        self.assertTrue(oir_con(None, fuerte))

    def test_calibrar_sube_el_umbral_con_el_ruido_de_la_habitacion(self):
        detector = Palmadas()
        detector.calibrar(5000.0)
        secuencia = palmada(0.20, nivel=9000.0) + ruido(0.30, 0.90) + palmada(0.90, nivel=9000.0)
        respuestas = [detector.alimentar(trozo(pico), ahora=instante)
                      for pico, instante in secuencia]
        # 5 × 5000 = 25000 de umbral: 9000 ya no pasa
        self.assertFalse(any(respuestas))

    def test_un_golpe_debajo_del_minimo_no_abre(self):
        # Medido en la habitación real: un ruido suelto de 5748 pasaba con
        # el mínimo viejo y bastaban dos en el intervalo para reabrir el
        # agente solo. El mínimo actual deja esos golpes fuera.
        secuencia = (ruido(0.0, 0.20) + palmada(0.20, nivel=6000.0)
                     + ruido(0.30, 0.90) + palmada(0.90, nivel=6000.0))
        self.assertFalse(any(oir(secuencia)))

    def test_una_palmada_de_verdad_sigue_abriendo(self):
        secuencia = (ruido(0.0, 0.20) + palmada(0.20, nivel=9000.0)
                     + ruido(0.30, 0.90) + palmada(0.90, nivel=9000.0))
        self.assertTrue(any(oir(secuencia)))


class ReposoPalmadasTest(unittest.TestCase):
    """Tras cerrar (o abrir) el agente, el micro se ignora un rato."""

    def test_al_cerrar_no_escucha_durante_el_reposo(self):
        reposo = ReposoPalmadas(segundos=8.0)
        reposo.observar(True, ahora=100.0)      # el agente está abierto
        reposo.observar(False, ahora=105.0)     # el usuario lo cierra

        self.assertFalse(reposo.puede_escuchar(105.0))
        self.assertFalse(reposo.puede_escuchar(112.9))
        self.assertTrue(reposo.puede_escuchar(113.0))

    def test_mientras_esta_abierto_no_hay_reposo(self):
        reposo = ReposoPalmadas(segundos=8.0)
        self.assertTrue(reposo.puede_escuchar(0.0))

        reposo.observar(True, ahora=100.0)

        self.assertTrue(reposo.puede_escuchar(100.5))

    def test_al_abrir_tambien_hace_margen(self):
        reposo = ReposoPalmadas(segundos=8.0)
        reposo.abrir(ahora=200.0)

        self.assertFalse(reposo.puede_escuchar(205.0))
        self.assertTrue(reposo.puede_escuchar(208.0))

    def test_si_nunca_estuvo_abierto_no_marca_reposo(self):
        reposo = ReposoPalmadas(segundos=8.0)
        reposo.observar(False, ahora=50.0)

        self.assertTrue(reposo.puede_escuchar(50.1))


class UnaSolaInstanciaTest(unittest.TestCase):
    """No se abren dos ventanas apiladas (cerrar una dejaba la otra encima)."""

    def test_no_abre_una_copia_si_ya_hay_agente(self):
        with patch("voz.agente_vivo", return_value=True), \
                patch("voz.traer_al_frente", return_value=True) as frente, \
                patch("voz.marcar_agente_vivo") as marcar:
            voz.arrancar(None, None)

        frente.assert_called_once()
        marcar.assert_not_called()

    def test_sin_agente_previo_sigue_arrancando_normal(self):
        with patch("voz.agente_vivo", return_value=False), \
                patch("voz.marcar_agente_vivo") as marcar:
            # se corta antes de montar el orbe: sin ventana no hay nada que
            # probar aquí, con lo que basta con comprobar que se marca vivo
            with patch("voz.Orbe") as orbe:
                orbe.side_effect = RuntimeError("corte de prueba")
                try:
                    voz.arrancar(None, None)
                except RuntimeError:
                    pass

        marcar.assert_called_once()


class MicroPalmadasTest(unittest.TestCase):
    """El micro preferido puede estar mudo (cascos desconectados)."""

    @staticmethod
    def sondeo(picos, muertos=()):
        def medir(indice):
            if indice in muertos:
                raise OSError("no se pudo abrir")
            return picos.get(indice, 0.0)
        return medir

    def test_si_el_micro_preferido_capta_no_se_toca(self):
        picos = {1: 9000.0, 2: 12000.0}
        elegido = elegir_micro_palmadas(1, self.sondeo(picos), [1, None, 2])
        self.assertEqual(elegido, 1)

    def test_si_esta_mudo_se_pasa_al_siguiente_que_capta(self):
        picos = {2: 11000.0}
        elegido = elegir_micro_palmadas(1, self.sondeo(picos), [1, None, 2])
        self.assertEqual(elegido, 2)

    def test_un_micro_que_no_se_puede_abrir_se_salta(self):
        picos = {2: 11000.0}
        elegido = elegir_micro_palmadas(1, self.sondeo(picos, muertos=(None,)), [1, None, 2])
        self.assertEqual(elegido, 2)

    def test_si_ninguno_capta_se_queda_en_el_preferido(self):
        elegido = elegir_micro_palmadas(1, self.sondeo({}), [1, None, 2])
        self.assertEqual(elegido, 1)

    def test_un_susurro_sano_no_lo_tomo_por_micro_mudo(self):
        soplo = {1: MICRO_MUERTO, 2: 9000.0}
        mudo = {1: MICRO_MUERTO - 1, 2: 9000.0}
        self.assertEqual(elegir_micro_palmadas(1, self.sondeo(soplo), [1, 2]), 1)
        self.assertEqual(elegir_micro_palmadas(1, self.sondeo(mudo), [1, 2]), 2)

    def test_una_entrada_atascada_en_un_valor_no_sirve(self):
        # hay capturas rotas que devuelven 26881 (o -32768) en todos los
        # trozos: no es sonido y con eso el umbral se dispararía a 65536
        self.assertEqual(pico_sano([26881.0] * 8), 0.0)
        self.assertEqual(pico_sano([32768.0] * 8), 0.0)

    def test_un_micro_que_susurra_sigue_vivo(self):
        # El MHW-100 tiene compuerta: en una habitación callada devuelve 2 o 3.
        # Con el corte en 50 se tomaba por muerto y el vigilante se iba a
        # escuchar a otro micro, que no oye las palmadas.
        self.assertGreaterEqual(pico_sano([1.0, 2.0, 1.0, 3.0]), MICRO_MUERTO)

    def test_un_micro_mudo_se_queda_en_cero(self):
        self.assertLess(pico_sano([0.0] * 8), MICRO_MUERTO)
        self.assertLess(pico_sano([]), MICRO_MUERTO)

    def test_un_micro_que_capta_da_su_pico_real(self):
        self.assertEqual(pico_sano([2.0, 1400.0, 3.0, 1450.0]), 1450.0)
        self.assertEqual(pico_sano([]), 0.0)


class PalmadasEntornoTest(unittest.TestCase):
    def test_umbral_fijo_viene_de_agente_palmada_umbral(self):
        os.environ["AGENTE_PALMADA_UMBRAL"] = "3500"
        try:
            self.assertEqual(umbral_palmadas(), 3500.0)
        finally:
            del os.environ["AGENTE_PALMADA_UMBRAL"]
        self.assertIsNone(umbral_palmadas())

    def test_un_umbral_que_no_es_numero_se_ignora(self):
        os.environ["AGENTE_PALMADA_UMBRAL"] = "fuerte"
        try:
            self.assertIsNone(umbral_palmadas())
        finally:
            del os.environ["AGENTE_PALMADA_UMBRAL"]

    def test_instalar_y_desinstalar_el_acceso_directo(self):
        with tempfile.TemporaryDirectory() as carpeta:
            destino = instalar_palmadas(Path(carpeta))
            self.assertTrue(destino.exists())
            self.assertTrue(destino.name.startswith("Agente - palmadas"))
            texto = destino.read_bytes()          # el .lnk guarda la orden en UTF-16
            self.assertIn("--vigilar-palmadas".encode("utf-16-le"), texto)

            self.assertTrue(desinstalar_palmadas(Path(carpeta)))
            self.assertFalse(destino.exists())
            self.assertFalse(desinstalar_palmadas(Path(carpeta)))


class CalibracionTest(unittest.TestCase):
    """La medición del fondo no puede fiarse de una entrada rota."""

    class Fuente:
        """Lee siempre los mismos picos, o alternando, según se le pida."""

        def __init__(self, picos):
            self._picos = list(picos)
            self._i = 0
            fuente = self

            class _Flujo:
                def read(self, n):
                    pico = fuente._picos[fuente._i % len(fuente._picos)]
                    fuente._i += 1
                    return trozo(pico)[:n * 2]

            self.stream = _Flujo()

    def test_una_entrada_atascada_no_calibra_un_umbral_imposible(self):
        # Un valor fijo de 26881 daría fondo 32768 y umbral 65536: con eso
        # no entraría ni una palmada pegada al micro.
        self.assertEqual(_pico_de_fondo(self.Fuente([26881.0]), BLOQUE,
                                        segundos=0.05), 0.0)

    def test_un_fondo_normal_se_mide_como_antes(self):
        fondo = _pico_de_fondo(self.Fuente([60.0, 1400.0, 60.0, 1450.0]),
                               BLOQUE, segundos=0.05)
        self.assertEqual(fondo, 1450.0)


if __name__ == "__main__":
    unittest.main()
