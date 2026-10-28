import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from demos.model.adapter import (
    PROTOCOL_TOKENS,
    AdapterError,
    RealtimeVenusOmniAdapter,
    SessionBusy,
)
from demos.model.server import _ServerConfig, create_app
from demos.model.wire import PROTOCOL_VERSION


def omni_adapter():
    return RealtimeVenusOmniAdapter(
        MagicMock(),
        special_token_ids={token: index for index, token in enumerate(PROTOCOL_TOKENS)},
        model_revision="realtime-venus-omni-v1",
        model_name="Realtime-Venus-Omni",
    )


class SessionOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = omni_adapter()

    async def asyncTearDown(self):
        session = self.adapter._session
        if session is not None and not session.closed:
            await self.adapter.close_session(
                {"session_id": session.session_id, "incarnation": session.incarnation}
            )

    async def open(self, session_id, model="Realtime-Venus-Omni"):
        return await self.adapter.open_session(
            {"session_id": session_id, "model": model, "protocol_version": PROTOCOL_VERSION}
        )

    async def test_open_rejects_other_model(self):
        with self.assertRaises(AdapterError):
            await self.open("A", model="Realtime-Venus-Audio")

    async def test_live_session_stays_busy(self):
        await self.open("A")
        with self.assertRaises(SessionBusy):
            await self.open("B")

    async def test_requests_keep_session_alive(self):
        opened = await self.open("A")
        self.adapter._session.last_seen -= 20
        with self.assertRaises(TimeoutError):
            await self.adapter.next_output_step(
                "A", incarnation=opened["incarnation"], timeout_s=0
            )
        with self.assertRaises(SessionBusy):
            await self.open("B")

    async def test_stale_session_is_replaced(self):
        await self.open("A")
        old = self.adapter._session
        old.last_seen -= 20
        opened = await self.open("B")
        self.assertTrue(old.closed)
        self.assertEqual(self.adapter._session.session_id, "B")
        self.assertEqual(opened["incarnation"], old.incarnation + 1)


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
