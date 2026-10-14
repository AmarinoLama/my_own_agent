"""Pruebas HTTP del servidor local Cardpilot y su cola de importación."""
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import cardmarket


class CardmarketServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_patch = patch.object(cardmarket, "ROOT", Path(self.temp.name))
        self.root_patch.start()
        web_root = Path(self.temp.name) / "cardmarket-angular" / "dist" / "cardmarket-angular" / "browser"
        web_root.mkdir(parents=True)
        (web_root / "index.html").write_text("<html><app-root>Cardpilot Angular</app-root></html>", encoding="utf-8")
        extension_root = Path(self.temp.name) / "cardmarket-extension"
        extension_root.mkdir()
        (extension_root / "manifest.json").write_text("{}", encoding="utf-8")
        (extension_root / "popup.js").write_text("// test", encoding="utf-8")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), cardmarket.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.root_patch.stop()
        self.temp.cleanup()

    def test_health_endpoint_confirma_que_el_servidor_responde(self):
        with urlopen(f"{self.base}/api/health") as response:
            self.assertEqual(200, response.status)
            self.assertEqual({"ok": True, "app": "Cardpilot"}, json.load(response))

    def test_extension_enqueue_y_app_consume_importacion(self):
        payload = json.dumps({
            "person": "Ana", "pageTitle": "Mi lista", "sourceUrl": "https://www.cardmarket.com/en/Magic/Wants/123",
            "wants": [{"persona": "Ana", "carta": "Sol Ring", "cantidad": 2, "trend": None}],
            "offers": [],
        }).encode()
        request = Request(
            f"{self.base}/api/import", data=payload, method="POST",
            headers={"Content-Type": "application/json", "Origin": "moz-extension://cardpilot-test"},
        )
        with urlopen(request) as response:
            self.assertEqual("moz-extension://cardpilot-test", response.headers["Access-Control-Allow-Origin"])
            self.assertEqual({"ok": True, "wants": 1, "offers": 0}, json.load(response))

        poll = Request(f"{self.base}/api/import/next", headers={"Origin": self.base})
        with urlopen(poll) as response:
            imported = json.load(response)
        self.assertEqual("Ana", imported["person"])
        self.assertEqual("Sol Ring", imported["wants"][0]["carta"])
        with urlopen(poll) as response:
            self.assertEqual({"wants": [], "offers": []}, json.load(response))

    def test_frontend_serves_build_angular_and_extension_zip(self):
        with urlopen(self.base + "/") as response:
            self.assertEqual(200, response.status)
            self.assertIn("Cardpilot Angular", response.read().decode())
        with urlopen(self.base + "/extension.zip") as response:
            self.assertEqual(200, response.status)
            with tempfile.TemporaryFile() as archive_file:
                archive_file.write(response.read())
                archive_file.seek(0)
                import zipfile
                with zipfile.ZipFile(archive_file) as archive:
                    self.assertIn("cardmarket-extension/manifest.json", archive.namelist())

    def test_rechaza_importacion_http_normal_sin_origen_extension(self):
        payload = json.dumps({"wants": [], "offers": []}).encode()
        request = Request(
            f"{self.base}/api/import", data=payload, method="POST",
            headers={"Content-Type": "application/json", "Origin": self.base},
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(403, error.exception.code)

    def test_solo_app_local_puede_consultar_cola(self):
        request = Request(f"{self.base}/api/import/next", headers={"Origin": "https://example.com"})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(403, error.exception.code)


if __name__ == "__main__":
    unittest.main()
