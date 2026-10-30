import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from demos.model.client import RemoteModelUnavailable
from demos.server.session import WebAgentSession


class ModelErrorCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_reports_model_code_to_browser(self):
        websocket = MagicMock(accept=AsyncMock(), send_json=AsyncMock(), close=AsyncMock())
        factory = AsyncMock(side_effect=RemoteModelUnavailable("model_busy", "busy"))
        await WebAgentSession(websocket, factory, mode="camera").run()
        sent = [call.args[0] for call in websocket.send_json.await_args_list]
        self.assertIn("model_busy", [message.get("code") for message in sent])

    async def test_link_lost_behind_a_failed_host_keeps_its_code(self):
        # Another caller met the dead link first; the host now fails with its own error.
        failed = RuntimeError("VenusOmni serving host is in a failed state")
        failed.__cause__ = RemoteModelUnavailable("model_link_lost", "link down")
        host = MagicMock(
            begin_backend_turn=AsyncMock(return_value=False),
            next_output=AsyncMock(side_effect=failed),
            close=AsyncMock(),
        )
        host.session.list_works.return_value = []
        websocket = MagicMock(
            accept=AsyncMock(),
            send_json=AsyncMock(),
            close=AsyncMock(),
            receive_json=AsyncMock(side_effect=asyncio.Event().wait),
        )
        resources = MagicMock(model="Realtime-Venus-Omni", close=AsyncMock())
        with patch("demos.server.session.VenusOmniServingHost.open", AsyncMock(return_value=host)):
            await WebAgentSession(websocket, AsyncMock(return_value=resources), mode="camera").run()
        fatal = [call.args[0] for call in websocket.send_json.await_args_list
                 if call.args[0]["type"] == "fatal_error"]
        self.assertEqual([message.get("code") for message in fatal], ["model_link_lost"])


if __name__ == "__main__":
    unittest.main()
