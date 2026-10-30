import asyncio
import base64
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from demos.server.session import WebAgentSession
from harness.bridge.host import AudioDisposition


def make_session():
    websocket = MagicMock(accept=AsyncMock(), send_json=AsyncMock(), close=AsyncMock())
    return WebAgentSession(websocket, AsyncMock(), mode="camera"), websocket


class WebLinkTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_input_is_dropped_while_link_down(self):
        session, _ = make_session()
        session.host = MagicMock(append_audio=AsyncMock())
        message = {
            "format": "pcm16",
            "sample_rate": 16000,
            "data": base64.b64encode(b"\0" * 640).decode(),
        }
        session._link_up = False
        await session.handle("audio", message)
        session.host.append_audio.assert_not_awaited()
        session._link_up = True
        await session.handle("audio", message)
        session.host.append_audio.assert_awaited_once()

    async def test_link_change_is_reported(self):
        session, websocket = make_session()
        await session._link_changed(False)
        websocket.send_json.assert_awaited_with({"type": "model_link", "state": "down"})
        self.assertFalse(session._link_up)

    async def test_outage_logs_the_audio_it_dropped(self):
        session, _ = make_session()
        await session._link_changed(False)
        await session.handle("audio", pcm_message(b"\0" * 640))  # 20 ms
        with self.assertLogs("demos.server.session", "INFO") as logs:
            await session._link_changed(True)
        self.assertIn("dropped_audio_ms=20", logs.output[0])

    async def test_each_second_of_input_logs_its_loudness(self):
        session, _ = make_session()
        session.host = MagicMock(append_audio=AsyncMock())
        tenth = struct.pack("<h", 3277) * 320  # 20 ms at 1/10 of full scale
        with self.assertLogs("demos.server.session", "INFO") as logs:
            for index in range(50):
                await session.handle("audio", {**pcm_message(tenth), "playing": index == 7})
        self.assertEqual(len(logs.output), 1)
        self.assertIn("rms_dbfs=-20.0 playing=True", logs.output[0])

    async def test_turn_logs_what_happened_to_its_audio(self):
        session, _ = make_session()

        def step(disposition, text, finished):
            return SimpleNamespace(
                output=SimpleNamespace(turn_finished=finished, generation_id="g"),
                model_step=SimpleNamespace(visible_text=text),
                audio_disposition=disposition,
                caused_by_work_id="",
                utterance_id="u",
            )

        steps = [
            step(AudioDisposition.NONE, "", True),  # a listen unit: no line
            step(AudioDisposition.QUEUED, "好", False),
            step(AudioDisposition.SUPPRESSED, "", True),
        ]
        session.host = MagicMock(
            begin_backend_turn=AsyncMock(return_value=False),
            next_output=AsyncMock(side_effect=[*steps, asyncio.CancelledError]),
        )
        with self.assertLogs("demos.server.session", "INFO") as logs, self.assertRaises(asyncio.CancelledError):
            await session.output()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("queued=1 suppressed=1 chars=1", logs.output[0])

    def test_unchanged_work_is_logged_once(self):
        session, _ = make_session()
        work = {"work_id": "w", "capability": "general", "state": "running", "phase": "running",
                "delivery_status": "pending", "feedback": [], "created_at": None}
        with self.assertLogs("demos.server.session", "INFO") as logs:
            session._log_work_changes([work])
            session._log_work_changes([work])
            session._log_work_changes([{**work, "state": "completed"}])
        self.assertEqual(len(logs.output), 2)
        self.assertIn("state=completed", logs.output[1])


def pcm_message(data):
    return {"format": "pcm16", "sample_rate": 16000, "data": base64.b64encode(data).decode()}


if __name__ == "__main__":
    unittest.main()
