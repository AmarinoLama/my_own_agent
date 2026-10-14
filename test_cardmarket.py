"""Pruebas del optimizador local de compras Cardmarket."""
import unittest
import zipfile
from io import BytesIO

from cardmarket import build_xlsx, optimize


class CardmarketOptimizerTests(unittest.TestCase):
    def test_repartir_envio_hace_mejor_la_tienda_compartida(self):
        wants = [
            {"persona": "Ana", "carta": "Carta A", "cantidad": 1, "trend": "10"},
            {"persona": "Beto", "carta": "Carta B", "cantidad": 1, "trend": "5"},
            {"persona": "Cora", "carta": "Carta B", "cantidad": 1, "trend": "5"},
        ]
        offers = [
            {"vendedor": "Barato", "carta": "Carta A", "precio": "1", "cantidad": 1, "envio": "5"},
            {"vendedor": "Grupo", "carta": "Carta A", "precio": "3", "cantidad": 1, "envio": "1.50"},
            {"vendedor": "Grupo", "carta": "Carta B", "precio": "1", "cantidad": 2, "envio": "1.50"},
        ]

        result = optimize(wants, offers)

        self.assertTrue(result["optimo_probado"])
        self.assertEqual(3, result["cubiertas"])
        self.assertEqual(["Grupo"], [seller["vendedor"] for seller in result["vendedores"]])
        self.assertEqual(6.5, result["total"])
        self.assertEqual(1.5, result["envio"])
        self.assertEqual(1.5, sum(person["envio"] for person in result["personas"]))
        self.assertEqual(3, result["vendedores"][0]["compradores"])

    def test_minimiza_precio_total_cuando_no_hay_compra_compartida(self):
        wants = [{"persona": "Ana", "carta": "Carta A", "cantidad": 1, "trend": "10"}]
        offers = [
            {"vendedor": "A", "carta": "Carta A", "precio": "1", "cantidad": 1, "envio": "2"},
            {"vendedor": "B", "carta": "Carta A", "precio": "2", "cantidad": 1, "envio": "0.5"},
        ]
        result = optimize(wants, offers)
        self.assertEqual("B", result["vendedores"][0]["vendedor"])
        self.assertEqual(2.5, result["total"])
        self.assertEqual(7.5, result["beneficio"])

    def test_stock_insuficiente_informa_copias_pendientes(self):
        result = optimize(
            [{"persona": "Ana", "carta": "Carta A", "cantidad": 2, "trend": "4"}],
            [{"vendedor": "A", "carta": "Carta A", "precio": "1", "cantidad": 1, "envio": "1"}],
        )
        self.assertEqual(1, result["cubiertas"])
        self.assertEqual(1, result["sin_cubrir"])
        self.assertEqual([{"persona": "Ana", "carta": "Carta A", "cantidad": 1}], result["pendientes"])

    def test_si_falta_stock_prioriza_wants_con_mayor_trend(self):
        result = optimize(
            [
                {"persona": "Ana", "carta": "Carta A", "cantidad": 1, "trend": "1"},
                {"persona": "Beto", "carta": "Carta A", "cantidad": 1, "trend": "10"},
            ],
            [{"vendedor": "A", "carta": "Carta A", "precio": "2", "cantidad": 1, "envio": "0"}],
        )
        self.assertEqual(1, result["cubiertas"])
        self.assertEqual(["Beto"], [line["persona"] for line in result["compras"]])
        self.assertEqual(8, result["beneficio"])

    def test_rechaza_envios_inconsistentes_para_mismo_vendedor(self):
        wants = [{"persona": "Ana", "carta": "Carta A", "cantidad": 1, "trend": "1"}]
        offers = [
            {"vendedor": "A", "carta": "Carta A", "precio": "1", "cantidad": 1, "envio": "1"},
            {"vendedor": "a", "carta": "Carta B", "precio": "1", "cantidad": 1, "envio": "2"},
        ]
        with self.assertRaisesRegex(ValueError, "varios importes de envío"):
            optimize(wants, offers)

    def test_exporta_excel_xlsx_con_hojas_de_resumen_y_detalle(self):
        result = optimize(
            [{"persona": "Ana", "carta": "Carta A", "cantidad": 1, "trend": "2"}],
            [{"vendedor": "A", "carta": "Carta A", "precio": "1", "cantidad": 1, "envio": "0.2"}],
        )
        contents = build_xlsx(result)
        with zipfile.ZipFile(BytesIO(contents)) as workbook:
            self.assertIn("xl/worksheets/sheet1.xml", workbook.namelist())
            summary = workbook.read("xl/worksheets/sheet1.xml").decode()
            detail = workbook.read("xl/worksheets/sheet2.xml").decode()
            self.assertIn("Optimizador de compra Cardmarket", summary)
            self.assertIn("Carta A", detail)


if __name__ == "__main__":
    unittest.main()
