"""Pruebas del thinking orb: iconos y mascota, sin abrir ventanas."""

import tempfile
import unittest
from pathlib import Path

try:
    import numpy  # noqa: F401
    from PIL import Image
except ImportError:
    Image = None

import orbe_art


@unittest.skipIf(Image is None, "Pillow/numpy no están instalados")
class OrbeArteTests(unittest.TestCase):
    def test_mascota_es_rgba_con_fondo_transparente(self):
        imagen = orbe_art.pintar("piensa", fase=0.4)
        self.assertEqual((orbe_art.ANCHO_MASCOTA, orbe_art.ALTO_MASCOTA), imagen.size)
        self.assertEqual("RGBA", imagen.mode)
        self.assertEqual(0, imagen.getpixel((0, 0))[3])

    def test_estados_se_dibujan_distintos(self):
        estados = tuple(orbe_art.CUADROS)
        bytes_ = [orbe_art.pintar(estado, fase=0.35).tobytes() for estado in estados]
        self.assertEqual(len(estados), len(set(bytes_)))

    def test_la_animacion_avanza_entre_cuadros(self):
        primero = orbe_art.pintar("piensa", fase=0.0).tobytes()
        segundo = orbe_art.pintar("piensa", fase=0.4).tobytes()
        self.assertNotEqual(primero, segundo)

    def test_el_icono_lleva_varios_tamanos(self):
        with tempfile.TemporaryDirectory() as carpeta:
            ruta = orbe_art.escribir_icono(Path(carpeta) / "orbe.ico")
            with Image.open(ruta) as icono:
                self.assertIn((16, 16), icono.info["sizes"])
                self.assertIn((256, 256), icono.info["sizes"])
                icono.size = (256, 256)
                icono.load()
                self.assertEqual("RGBA", icono.convert("RGBA").mode)

    def test_el_brillo_no_toca_el_borde_del_lienzo(self):
        for lado in (16, 48, 125):
            imagen = orbe_art.pintar("piensa", ancho=lado, alto=lado, detalle=lado >= 48)
            pixeles = imagen.load()
            esquinas = [pixeles[0, 0], pixeles[lado - 1, 0],
                        pixeles[0, imagen.height - 1], pixeles[lado - 1, imagen.height - 1]]
            self.assertTrue(all(px[3] <= 2 for px in esquinas), esquinas)
            borde = [pixeles[x, 0][3] for x in range(lado)]
            self.assertLessEqual(max(borde), 16)


if __name__ == "__main__":
    unittest.main()
