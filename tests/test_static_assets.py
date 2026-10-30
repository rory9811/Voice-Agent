import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from demos.server.app import create_app

STATIC = Path(__file__).resolve().parents[1] / "demos" / "static"


class StaticAssetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        app = create_app(settings_path=Path(directory.name) / "settings.json", model_type="omni", tokenizer=None, model_server_url="http://unused")
        self.client = TestClient(app, base_url="http://localhost")

    def test_scripts_are_revalidated_on_every_load(self):
        for name in ("app.js", "capture.js", "audio.js", "capture-worklet.js", "style.css"):
            with self.subTest(name=name):
                response = self.client.get("/assets/" + name)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["cache-control"], "no-cache")

    def test_unchanged_script_is_not_downloaded_again(self):
        etag = self.client.get("/assets/capture.js").headers["etag"]
        response = self.client.get("/assets/capture.js", headers={"if-none-match": etag})
        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.headers["cache-control"], "no-cache")

    def test_page_loads_assets_from_the_revalidated_path(self):
        page = self.client.get("/").text
        self.assertIn('src="/assets/app.js"', page)
        self.assertIn('href="/assets/style.css"', page)

    def test_no_file_carries_a_hand_kept_version(self):
        for path in [*STATIC.glob("*.js"), STATIC / "index.html"]:
            with self.subTest(path=path.name):
                self.assertNotIn("?v=", path.read_text())
                self.assertNotIn("/static/", path.read_text())


if __name__ == "__main__":
    unittest.main()
