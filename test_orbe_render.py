"""Pruebas de renderizado del orbe sin abrir ventanas ni usar micrófono."""

import io
import math
import unittest
from unittest.mock import patch

import voz


class OrbeRenderTests(unittest.TestCase):
    def renderizar(self, estado, tiempo=0.0, nivel=0.0):
        if voz.Image is None:
            self.skipTest("Pillow no está instalado")

        class LienzoFalso:
            def itemconfig(self, _item, **kwargs):
                self.imagen = kwargs["image"]

        orbe = voz.Orbe.__new__(voz.Orbe)
        orbe.estado_actual = estado
        orbe.tiempo = tiempo
        orbe.nivel_visible = nivel
        orbe.lienzo = LienzoFalso()
        orbe.sprite = 1
        captura = {}

        def capturar(data):
            captura["ppm"] = data
            return object()

        with patch("voz.tk.PhotoImage", side_effect=capturar):
            orbe._pintar()

        return voz.Image.open(io.BytesIO(captura["ppm"])).convert("RGB")

    @staticmethod
    def _centro():
        return voz.Orbe.ANCHO // 2, voz.Orbe.ALTO_PASTILLA // 2

    def _distancias_al_centro(self, imagen, radio=25):
        """Distancias al centro de los píxeles pintados de los puntos (ni color
        clave —lo que queda fuera de la ventana— ni la pastilla negra)."""
        cx, cy = self._centro()
        return [
            math.hypot(x - cx, y - cy)
            for y in range(imagen.height)
            for x in range(imagen.width)
            if math.hypot(x - cx, y - cy) < radio
            and imagen.getpixel((x, y)) not in (voz.Orbe.CLAVE_RGB, tuple(voz.Orbe.NEGRO))
        ]

    def test_barra_notch_puesta_en_el_borde_superior(self):
        imagen = self.renderizar("inactivo")
        self.assertEqual((voz.Orbe.ANCHO, voz.Orbe.ALTO), imagen.size)
        clave = voz.Orbe.CLAVE_RGB
        negro = tuple(voz.Orbe.NEGRO)
        # Lo que queda fuera de la pastilla es color clave (ventana transparente)
        self.assertEqual(clave, imagen.getpixel((0, 0)))
        self.assertEqual(clave, imagen.getpixel((voz.Orbe.ANCHO - 1, 0)))
        self.assertEqual(clave, imagen.getpixel((0, voz.Orbe.ALTO - 1)))
        self.assertEqual(clave, imagen.getpixel((voz.Orbe.ANCHO - 1, voz.Orbe.ALTO - 1)))
        # La pastilla es negra y llega pegada al borde superior de la pantalla
        self.assertEqual(negro, imagen.getpixel((voz.Orbe.ANCHO // 2, 0)))
        self.assertEqual(negro, imagen.getpixel((30, 40)))
        # Codo invertido izquierdo: el arco es cóncavo hacia la pastilla
        self.assertEqual(clave, imagen.getpixel((1, 0)))      # fuera del codo
        self.assertEqual(negro, imagen.getpixel((12, 0)))     # a ras del borde
        self.assertEqual(clave, imagen.getpixel((6, 6)))      # dentro del arco
        # y el derecho, simétrico
        self.assertEqual(clave, imagen.getpixel((voz.Orbe.ANCHO - 2, 0)))
        self.assertEqual(negro, imagen.getpixel((voz.Orbe.ANCHO - 16, 0)))
        self.assertEqual(clave, imagen.getpixel((voz.Orbe.ANCHO - 7, 6)))

    def test_pensando_dibuja_un_circulo_alrededor_del_centro(self):
        imagen = self.renderizar("pensando")
        cx, cy = self._centro()
        # Los 5 puntos giran a su alrededor: el centro queda para la pastilla
        self.assertEqual(tuple(voz.Orbe.NEGRO), imagen.getpixel((cx, cy)))
        self.assertGreater(len(imagen.getcolors(imagen.width * imagen.height) or []), 20)
        distancias = self._distancias_al_centro(imagen, radio=30)
        self.assertTrue(distancias)
        self.assertGreater(len(distancias), 200)
        # El hueco del centro se queda para la pastilla: los puntos dan vueltas
        # alrededor y su halo apenas roza el medio (borrosos, lo justo).
        hueco = [d for d in self._distancias_al_centro(imagen, radio=4)]
        self.assertLess(len(hueco), 12)
        self.assertLess(max(distancias), 30)

    def _pintados(self, imagen, condicion):
        """[(x, y)] de los píxeles del orbe (ni clave ni pastilla) que cumplen
        `condicion(rojo, verde, azul)`."""
        return [
            (x, y)
            for y in range(imagen.height)
            for x in range(imagen.width)
            if imagen.getpixel((x, y)) not in (voz.Orbe.CLAVE_RGB, tuple(voz.Orbe.NEGRO))
            and condicion(*imagen.getpixel((x, y)))
        ]

    def test_tu_voz_se_pinta_de_azul_y_alineada(self):
        imagen = self.renderizar("escuchando", tiempo=0.5, nivel=0.9)
        cx, cy = self._centro()
        azules = self._pintados(imagen, lambda r, g, b: b > r + 30)
        self.assertGreater(len(azules), 100)
        self.assertTrue(all(abs(y - cy) < 16 for _, y in azules))
        self.assertTrue(all(abs(x - cx) < 60 for x, _ in azules))

    def test_su_voz_se_pinta_de_naranja_y_alineada(self):
        imagen = self.renderizar("hablando", tiempo=0.5)
        cx, cy = self._centro()
        naranjas = self._pintados(imagen, lambda r, g, b: r > 140 and r > b + 60)
        self.assertGreater(len(naranjas), 100)
        self.assertTrue(all(abs(y - cy) < 16 for _, y in naranjas))
        self.assertTrue(all(abs(x - cx) < 60 for x, _ in naranjas))

    def test_en_pausa_se_ve_el_simbolo_de_pausa(self):
        imagen = self.renderizar("pausado", tiempo=0.5)
        cx, cy = self._centro()
        simbolo = [
            (x, y)
            for y in range(cy + 11, cy + 27)
            for x in range(cx - 10, cx + 10)
            if imagen.getpixel((x, y)) not in (voz.Orbe.CLAVE_RGB, tuple(voz.Orbe.NEGRO))
        ]
        self.assertTrue(simbolo)

    def test_estados_tienen_animaciones_visualmente_distintas(self):
        estados = ("inactivo", "escuchando", "pensando", "hablando", "error")
        imagenes = [self.renderizar(estado, tiempo=0.7, nivel=0.65) for estado in estados]
        bytes_imagen = [imagen.tobytes() for imagen in imagenes]
        self.assertEqual(len(estados), len(set(bytes_imagen)))

    def test_orbe_en_movimiento_cambia_entre_marcos(self):
        primero = self.renderizar("pensando", tiempo=0.2).tobytes()
        segundo = self.renderizar("pensando", tiempo=0.8).tobytes()
        self.assertNotEqual(primero, segundo)


if __name__ == "__main__":
    unittest.main()
