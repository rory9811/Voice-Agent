import os
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from demos.model.client import RemoteOmniServingPort
from demos.server.resources import fetch_model


def mock_clients(handler, created):
    real = httpx.Client

    def factory(**kwargs):
        created.append(kwargs)
        return real(transport=httpx.MockTransport(handler), **kwargs)

    return patch("demos.server.resources.httpx.Client", factory)


class FetchModelTests(unittest.TestCase):
    def test_fetch_model_reads_type_and_files(self):
        def handler(request):
            if request.url.path == "/healthz":
                return httpx.Response(200, json={"ready": True, "model_type": "audio"})
            if request.url.path == "/tokenizer":
                return httpx.Response(
                    200, json={"files": {"tokenizer.json": "{}", "vocab.json": "{}"}}
                )
            return httpx.Response(404)

        seen, created = {}, []

        def load(directory):
            seen["files"] = sorted(path.name for path in Path(directory).iterdir())
            return "tokenizer"

        with mock_clients(handler, created), patch(
            "transformers.Qwen2TokenizerFast.from_pretrained", side_effect=load
        ):
            info = fetch_model("http://127.0.0.1:8031")
        self.assertEqual(info.model_type, "audio")
        self.assertEqual(info.tokenizer, "tokenizer")
        self.assertEqual(seen["files"], ["tokenizer.json", "vocab.json"])
        self.assertTrue(created and all(kw.get("trust_env") is False for kw in created))

    def test_fetch_model_unreachable_names_tunnel(self):
        def refused(request):
            raise httpx.ConnectError("connection refused", request=request)

        def loading(request):
            return httpx.Response(503, json={"ready": False})

        for handler in (refused, loading):
            with self.subTest(handler=handler.__name__), mock_clients(handler, []):
                with self.assertRaises(RuntimeError) as caught:
                    fetch_model("http://127.0.0.1:8031")
                self.assertIn("ssh -N -L 8031:127.0.0.1:8031", str(caught.exception))
                self.assertIn("bash start.sh model", str(caught.exception))

    def test_model_client_ignores_proxy_env(self):
        with patch.dict(os.environ, {"ALL_PROXY": "socks5://127.0.0.1:1"}):
            port = RemoteOmniServingPort("http://127.0.0.1:8031")
        self.assertFalse(port._client.trust_env)


if __name__ == "__main__":
    unittest.main()
