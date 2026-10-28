import unittest
from unittest.mock import AsyncMock, MagicMock

import httpx

from demos.model.adapter import ModelMismatch
from demos.model.client import RemoteModelUnavailable, RemoteOmniServingPort
from demos.server.session import WebAgentSession
from harness.bridge.serving import OpenSession


def port_answering(handler):
    port = RemoteOmniServingPort("http://127.0.0.1:8031")
    port._client = httpx.AsyncClient(
        base_url="http://127.0.0.1:8031", transport=httpx.MockTransport(handler)
    )
    return port


class ModelErrorCodeTests(unittest.IsolatedAsyncioTestCase):
    async def open_code(self, handler):
        port = port_answering(handler)
        with self.assertRaises(RemoteModelUnavailable) as caught:
            await port.open_session(OpenSession(session_id="s", model="Realtime-Venus-Omni"))
        await port.aclose()
        return caught.exception.code

    async def test_busy_session_maps_to_model_busy(self):
        code = await self.open_code(lambda request: httpx.Response(
            409, json={"error": "a session is already open", "type": "SessionBusy"}))
        self.assertEqual(code, "model_busy")

    async def test_other_model_maps_to_model_mismatch(self):
        code = await self.open_code(lambda request: httpx.Response(
            400, json={"error": "model mismatch", "type": "ModelMismatch"}))
        self.assertEqual(code, "model_mismatch")

    async def test_transport_failure_maps_to_model_unreachable(self):
        def refused(request):
            raise httpx.ConnectError("connection refused", request=request)
        self.assertEqual(await self.open_code(refused), "model_unreachable")

    def test_adapter_mismatch_type_name_matches_client(self):
        self.assertEqual(ModelMismatch.__name__, "ModelMismatch")

    async def test_session_reports_model_code_to_browser(self):
        websocket = MagicMock(accept=AsyncMock(), send_json=AsyncMock(), close=AsyncMock())
        factory = AsyncMock(side_effect=RemoteModelUnavailable("model_busy", "busy"))
        await WebAgentSession(websocket, factory, mode="omni").run()
        sent = [call.args[0] for call in websocket.send_json.await_args_list]
        self.assertIn("model_busy", [message.get("code") for message in sent])


if __name__ == "__main__":
    unittest.main()
