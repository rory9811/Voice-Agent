import unittest
from unittest.mock import AsyncMock, MagicMock

from demos.model.client import RemoteModelUnavailable
from demos.server.session import WebAgentSession


class ModelErrorCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_reports_model_code_to_browser(self):
        websocket = MagicMock(accept=AsyncMock(), send_json=AsyncMock(), close=AsyncMock())
        factory = AsyncMock(side_effect=RemoteModelUnavailable("model_busy", "busy"))
        await WebAgentSession(websocket, factory, mode="omni").run()
        sent = [call.args[0] for call in websocket.send_json.await_args_list]
        self.assertIn("model_busy", [message.get("code") for message in sent])


if __name__ == "__main__":
    unittest.main()
