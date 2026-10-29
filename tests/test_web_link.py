import base64
import unittest
from unittest.mock import AsyncMock, MagicMock

from demos.server.session import WebAgentSession


def make_session():
    websocket = MagicMock(accept=AsyncMock(), send_json=AsyncMock(), close=AsyncMock())
    return WebAgentSession(websocket, AsyncMock(), mode="omni"), websocket


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


if __name__ == "__main__":
    unittest.main()
