import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from demos.model.adapter import PROTOCOL_TOKENS, RealtimeVenusOmniAdapter
from demos.model.server import _ServerConfig, create_app


def omni_adapter():
    return RealtimeVenusOmniAdapter(
        MagicMock(),
        special_token_ids={token: index for index, token in enumerate(PROTOCOL_TOKENS)},
        model_revision="realtime-venus-omni-v1",
        model_name="Realtime-Venus-Omni",
    )


class ServerDescriptionTests(unittest.TestCase):
    def test_tokenizer_endpoint_serves_only_data_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ["tokenizer.json", "tokenizer_config.json", "vocab.json",
                         "tokenization_x_fast.py", "model.safetensors"]:
                (root / name).write_text(f"content of {name}", encoding="utf-8")
            stub = SimpleNamespace(
                _model_revision="r", _session=None, health=AsyncMock(return_value={})
            )
            config = _ServerConfig(
                model_path=tmp, model_type="audio", model_name="Realtime-Venus-Audio"
            )
            with patch("demos.model.server._build_adapter", AsyncMock(return_value=stub)):
                with TestClient(create_app(config)) as client:
                    files = client.get("/tokenizer").json()["files"]
                    health = client.get("/healthz").json()
            self.assertEqual(set(files), {"tokenizer.json", "tokenizer_config.json", "vocab.json"})
            self.assertEqual(files["vocab.json"], "content of vocab.json")
            self.assertEqual(health["model_type"], "audio")
            self.assertEqual(health["model"], "Realtime-Venus-Audio")


if __name__ == "__main__":
    unittest.main()
