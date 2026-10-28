import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from demos.server.app import create_app


def app(tmp):
    return create_app(settings_path=Path(tmp)/'harness.json', model_type='omni', tokenizer=None,
                      model_server_url='http://unused')


class LocalHostTests(unittest.TestCase):
    def test_only_local_host_names_are_served(self):
        # A DNS-rebinding page arrives with its own host name but a matching Origin.
        with tempfile.TemporaryDirectory() as tmp:
            for base, status in [('http://localhost:9000', 200), ('http://127.0.0.1:8032', 200),
                                 ('http://evil.example:8032', 403)]:
                with self.subTest(base=base), TestClient(app(tmp), base_url=base) as client:
                    self.assertEqual(client.get('/api/status').status_code, status)

    def test_websocket_from_foreign_host_is_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with TestClient(app(tmp), base_url='http://evil.example:8032') as client:
                with self.assertRaises(WebSocketDisconnect), client.websocket_connect('ws://evil.example:8032/ws') as socket:
                    socket.receive_json()


if __name__ == "__main__":
    unittest.main()
