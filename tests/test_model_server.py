import asyncio
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from demos.model.adapter import PROTOCOL_TOKENS, RealtimeVenusOmniAdapter
from demos.model.server import _ServerConfig, create_app
from demos.model.wire import PROTOCOL_VERSION, decode_frame, encode_frame


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


def open_frame(session_id="A", model="Realtime-Venus-Omni"):
    return encode_frame({"type": "open", "protocol_version": PROTOCOL_VERSION, "session_id": session_id,
                         "model": model, "ack": 0, "consumed": 0})


def resume_frame(opened, ack=0):
    return encode_frame({"type": "resume", "session_id": opened["session_id"],
                         "incarnation": opened["incarnation"], "ack": ack, "consumed": 0})


def audio_frame(seq, event_seq):
    # A tenth of a second: acknowledged, but too short for the model to step.
    return encode_frame({"type": "audio", "seq": seq, "ack": 0, "consumed": 0, "event_seq": event_seq,
                         "sample_rate_hz": 16_000}, b"\0\0" * 1600)


CLOSE = encode_frame({"type": "close", "seq": 1, "ack": 0, "consumed": 0, "reason": "done"})


def receive(ws):
    return decode_frame(ws.receive_bytes())[0]


class SessionSocketTests(unittest.TestCase):
    @contextmanager
    def client(self, token=None):
        self.adapter = omni_adapter()
        with patch("demos.model.server._build_adapter", AsyncMock(return_value=self.adapter)):
            with TestClient(create_app(_ServerConfig(token=token))) as client:
                yield client

    @staticmethod
    def connect(client, token=None):
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        return client.websocket_connect("/session", headers=headers)

    def open(self, ws):
        ws.send_bytes(open_frame())
        opened = receive(ws)
        self.assertEqual(opened["type"], "opened")
        return opened

    def receive_error(self, ws, code):
        error = receive(ws)
        self.assertEqual((error["type"], error["code"]), ("error", code))
        self.assertIs(type(error.get("ack")), int)
        return error

    def assertClosed(self, ws, code):
        with self.assertRaises(WebSocketDisconnect) as caught:
            ws.receive_bytes()
        self.assertEqual(caught.exception.code, code)

    def test_session_socket_requires_token(self):
        with self.client(token="secret") as client:
            for token in (None, "wrong"):
                with self.subTest(token=token), self.connect(client, token) as ws:
                    self.assertClosed(ws, 1008)
            with self.connect(client, "secret") as ws:
                self.open(ws)

    def test_open_returns_capabilities_and_acks_input(self):
        with self.client() as client, self.connect(client) as ws:
            opened = self.open(ws)
            self.assertEqual(opened["incarnation"], 1)
            self.assertEqual(opened["capabilities"]["raw_token_mode"], "delta")
            ws.send_bytes(audio_frame(seq=1, event_seq=1))
            accepted = receive(ws)
            self.assertEqual(accepted["type"], "input_accepted")
            self.assertEqual((accepted["seq"], accepted["ack"], accepted["input_seq"]), (1, 1, 1))

    def test_resume_replays_unconfirmed_frames(self):
        with self.client() as client:
            with self.connect(client) as a:
                opened = self.open(a)
                a.send_bytes(audio_frame(seq=1, event_seq=1))
                self.assertEqual(receive(a)["seq"], 1)
            with self.connect(client) as b:
                b.send_bytes(resume_frame(opened, ack=0))
                self.assertEqual(receive(b), {"type": "resumed", "ack": 1})
                replayed = receive(b)
                self.assertEqual((replayed["type"], replayed["seq"]), ("input_accepted", 1))

    def test_resume_supersedes_a_live_connection(self):
        with self.client() as client, self.connect(client) as a:
            opened = self.open(a)
            with self.connect(client) as b:
                b.send_bytes(resume_frame(opened))
                self.assertEqual(receive(b)["type"], "resumed")
                self.assertClosed(a, 4001)

    def test_handshake_errors_are_coded(self):
        with self.client() as client:
            with self.connect(client) as ws:
                ws.send_bytes(open_frame(model="Realtime-Venus-Audio"))
                self.receive_error(ws, "model_mismatch")
                self.assertClosed(ws, 1011)
            with self.connect(client) as a:
                self.open(a)
                with self.connect(client) as b:
                    b.send_bytes(open_frame(session_id="B"))
                    self.receive_error(b, "model_busy")
            with self.connect(client) as ws:
                ws.send_bytes(resume_frame({"session_id": "gone", "incarnation": 9}))
                self.receive_error(ws, "session_lost")

    def test_client_close_drains_then_ends_normally(self):
        with self.client() as client, self.connect(client) as ws:
            self.open(ws)
            ws.send_bytes(CLOSE)
            self.assertEqual(receive(ws)["type"], "closed")
            self.assertClosed(ws, 1000)

    def test_failed_close_reports_model_error(self):
        with self.client() as client, self.connect(client) as ws:
            self.open(ws)
            self.adapter._official.set_session_stop.side_effect = RuntimeError("stop failed")
            ws.send_bytes(CLOSE)
            self.assertIn("stop failed", self.receive_error(ws, "model_error")["error"])
            self.assertClosed(ws, 1011)

    def test_protocol_error_ends_the_session(self):
        with self.client() as client:
            with self.connect(client) as ws:
                self.open(ws)
                ws.send_bytes(audio_frame(seq=1, event_seq=2))  # event_seq must start at 1
                self.receive_error(ws, "protocol")
                self.assertClosed(ws, 1011)
            with self.connect(client) as ws:
                ws.send_bytes(b"\0")  # shorter than the length prefix
                self.receive_error(ws, "protocol")
                self.assertClosed(ws, 1011)
            with self.connect(client) as ws:
                self.open(ws)  # the broken session gave up the model

    def test_protocol_error_closes_the_session_when_the_peer_is_gone(self):
        # bad_seq 1: the error frame is the first send that fails. bad_seq 2: an
        # acknowledgement is being sent when the reader meets the bad frame.
        for bad_seq in (1, 2):
            with self.subTest(bad_seq=bad_seq), self.client() as client, self.connect(client) as ws:
                self.open(ws)
                sess = self.adapter._session

                async def peer_gone(_ws, _data):
                    while sess.inbox.last < bad_seq:  # the reader has not met the bad frame yet
                        await asyncio.sleep(0.005)
                    raise WebSocketDisconnect(1006)

                detached = threading.Event()
                real_detach = self.adapter.detach
                self.adapter.detach = lambda s, link: (real_detach(s, link), detached.set())
                with patch.object(WebSocket, "send_bytes", peer_gone):
                    for seq in range(1, bad_seq):
                        ws.send_bytes(audio_frame(seq=seq, event_seq=seq))
                    ws.send_bytes(audio_frame(seq=bad_seq, event_seq=bad_seq + 1))
                    self.assertTrue(detached.wait(5))
                # Resuming it would confirm a frame that was never applied.
                self.assertTrue(sess.closed)

if __name__ == "__main__":
    unittest.main()
