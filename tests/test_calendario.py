"""Pruebas locales aisladas del almacenamiento de calendario."""

import tempfile
import unittest
from pathlib import Path

from calendario import Calendario


class CalendarioTests(unittest.TestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.calendario = Calendario(Path(self.temporal.name) / "eventos.json")

    def tearDown(self):
        self.temporal.cleanup()

    def test_anade_y_consulta_evento_en_intervalo_inclusivo(self):
        anterior = self.calendario.anadir("Anterior", "2026-05-13")
        incluido = self.calendario.anadir("Dentista", "2026-05-14", "10:30", "Revisión")
        posterior = self.calendario.anadir("Posterior", "2026-05-15")

        self.assertEqual([incluido], self.calendario.consultar("2026-05-14", "2026-05-14"))
        self.assertEqual([anterior, incluido, posterior], self.calendario.consultar("2026-05-13", "2026-05-15"))

    def test_evento_de_todo_el_dia_y_hora_normalizada(self):
        todo_el_dia = self.calendario.anadir("Festivo", "2026-05-01")
        con_hora = self.calendario.anadir("Médico", "2026-05-02", "9:05")
        self.assertEqual("", todo_el_dia["hora"])
        self.assertEqual("09:05", con_hora["hora"])
        with self.assertRaises(ValueError):
            self.calendario.anadir("Cita", "2026-05-03", "9:05 am")

    def test_elimina_por_id_y_conserva_los_demas(self):
        borrar = self.calendario.anadir("Cita", "2026-06-01")
        conservar = self.calendario.anadir("Otra cita", "2026-06-02")

        self.assertEqual(borrar, self.calendario.eliminar(borrar["id"]))
        self.assertIsNone(self.calendario.eliminar("no-existe"))
        self.assertEqual([conservar], self.calendario.listar())

    def test_rechaza_fecha_hora_e_intervalo_invalidos(self):
        with self.assertRaises(ValueError):
            self.calendario.anadir("Cita", "mañana")
        with self.assertRaises(ValueError):
            self.calendario.anadir("Cita", "2026-6-1")
        with self.assertRaises(ValueError):
            self.calendario.anadir("Cita", "2026-06-01", "25:00")
        with self.assertRaises(ValueError):
            self.calendario.consultar("2026-06-02", "2026-06-01")


if __name__ == "__main__":
    unittest.main()
