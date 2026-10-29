import asyncio
import os
import socket
import time
import unittest
from unittest.mock import patch

from demos.model.client import (
    RemoteModelUnavailable,
    RemoteOmniServingPort,
    RemoteServingError,
    RemoteSessionClosed,
)
from harness.bridge.serving import (
    AudioAppend,
    CloseSession,
    InputAccepted,
    ModelOutputStep,
    OpenSession,
)
from link_proxy import LatencyProxy, serve_model
from test_model_driver import SPEAK
from test_model_server import omni_adapter

MODEL = "Realtime-Venus-Omni"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ModelLinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = omni_adapter()
        self.server = serve_model(self.adapter)
        self.url = await self.server.__aenter__()
        self.ports, self.proxies = [], []
        self.event_seq = 0

    async def asyncTearDown(self):
        for port in self.ports:
            await port.aclose()
        for proxy in self.proxies:
            await proxy.close()
        await self.server.__aexit__(None, None, None)

    async def proxied(self):
        proxy = LatencyProxy(int(self.url.rsplit(":", 1)[1]))
        self.proxies.append(proxy)
        return proxy, f"http://127.0.0.1:{await proxy.start()}"

    async def open(self, url=None, model=MODEL, **kwargs):
        port = RemoteOmniServingPort(url or self.url, timeout_s=5.0, **kwargs)
        self.ports.append(port)
        self.opened = await port.open_session(OpenSession(session_id="web-1", model=model))
        return port

    def send_audio(self, port, seconds=1.0):
        self.event_seq += 1
        start = (self.event_seq - 1) * 1000
        port.send_audio(AudioAppend(
            session_id=self.opened.session_id, incarnation=self.opened.incarnation,
            event_seq=self.event_seq, start_ms=start, end_ms=start + int(seconds * 1000),
            data=b"\0\0" * int(16_000 * seconds)))

    async def accepted_until(self, port, last_input_seq):
        """``input_seq`` of every InputAccepted taken until ``last_input_seq`` arrives."""
        seen = []
        while not seen or seen[-1] < last_input_seq:
            event = await port.next_event(timeout_s=5.0)
            if isinstance(event, InputAccepted):
                seen.append(event.input_seq)
        return seen

    async def close(self, port):
        await port.close_session(CloseSession(
            session_id=self.opened.session_id, incarnation=self.opened.incarnation))

    async def test_events_arrive_in_emit_order(self):
        self.adapter._model.streaming_generate.return_value = SPEAK
        port = await self.open()
        for _ in range(3):
            self.send_audio(port)
        accepted, outputs = [], []
        while len(outputs) < 3:
            event = await port.next_event(timeout_s=5.0)
            if isinstance(event, InputAccepted):
                accepted.append(event.input_seq)
            else:
                self.assertIsInstance(event, ModelOutputStep)
                self.assertLessEqual(event.input_seq_cutoff, max(accepted, default=0))
                outputs.append(event)
        # Each output consumed one second of audio, so its acknowledgement came first.
        self.assertEqual(accepted, [1, 2, 3])
        for step in outputs:
            self.assertIs(type(step.audio.data), bytes)
            self.assertEqual(len(step.audio.data) % 2, 0)
        await self.close(port)

    async def test_open_errors_map_to_codes(self):
        first = await self.open()
        for url, model, code in (
            (self.url, MODEL, "model_busy"),
            (self.url, "Realtime-Venus-Audio", "model_mismatch"),
            (f"http://127.0.0.1:{free_port()}", MODEL, "model_unreachable"),
        ):
            with self.subTest(code=code):
                with self.assertRaises(RemoteModelUnavailable) as caught:
                    await self.open(url, model=model)
                self.assertEqual(caught.exception.code, code)
        self.assertTrue(first.link_up)

    async def test_model_failure_on_open_is_reported(self):
        self.adapter._model.prepare.side_effect = RuntimeError("CUDA out of memory")
        with self.assertLogs("demos.model.server", "ERROR"), self.assertRaises(RemoteServingError) as caught:
            await self.open()
        self.assertNotIsInstance(caught.exception, RemoteModelUnavailable)
        self.assertIn("CUDA out of memory", str(caught.exception))

    async def test_wrong_token_is_named(self):
        async with serve_model(omni_adapter(), token="secret") as url:
            with self.assertRaises(RemoteServingError) as caught:
                await self.open(url, token="wrong")
            await self.close(await self.open(url, token="secret"))
        self.assertIn("VENUS_MODEL_TOKEN", str(caught.exception))

    async def test_link_ignores_proxy_env(self):
        proxies = {name: "socks5://127.0.0.1:1" for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")}
        with patch.dict(os.environ, proxies):
            for name in ("NO_PROXY", "no_proxy"):
                os.environ.pop(name, None)
            port = await self.open()
        await self.close(port)

    async def test_reconnect_resumes_without_loss(self):
        proxy, url = await self.proxied()
        changes = []

        async def changed(up):
            changes.append(up)

        port = await self.open(url)
        port.on_link_change = changed
        self.send_audio(port)
        self.send_audio(port)
        accepted = await self.accepted_until(port, 1)
        proxy.drop_all()
        self.send_audio(port)
        self.send_audio(port)
        accepted += await self.accepted_until(port, 4)
        self.assertEqual(accepted, [1, 2, 3, 4])
        self.assertEqual(changes, [False, True])
        self.assertTrue(port.link_up)
        await self.close(port)

    async def test_reconnect_deadline_ends_session(self):
        proxy, url = await self.proxied()
        port = await self.open(url)
        with patch("demos.model.client.RECONNECT_DEADLINE_S", 1.0):
            proxy.drop_all()
            proxy.refuse_for(3)
            with self.assertRaises(RemoteModelUnavailable) as caught:
                await port.next_event(timeout_s=5.0)
        self.assertEqual(caught.exception.code, "model_link_lost")

    async def test_close_during_outage_gives_up(self):
        proxy, url = await self.proxied()
        port = await self.open(url)
        waiting = asyncio.create_task(port.next_event())
        with patch("demos.model.client.CLOSE_TIMEOUT_S", 0.5):
            proxy.drop_all()
            proxy.refuse_for(3)
            async with asyncio.timeout(5):
                while port.link_up:
                    await asyncio.sleep(0.01)
            started = time.monotonic()
            with self.assertRaises(RemoteServingError):
                await self.close(port)
            self.assertLess(time.monotonic() - started, 1.0)
        with self.assertRaises(RemoteSessionClosed):
            await waiting

    async def test_writer_failure_ends_the_session(self):
        port = await self.open()
        unencodable = lambda kind, request: {"type": kind, "bad": object()}  # noqa: E731
        with patch("demos.model.client._header", unencodable):
            self.send_audio(port)
        with self.assertLogs("demos.model.client", "ERROR"), self.assertRaises(RemoteServingError):
            await port.next_event(timeout_s=2.0)

    async def test_gaps_are_logged_at_close(self):
        port = await self.open()
        self.send_audio(port)
        await self.accepted_until(port, 1)
        await self.close(port)
        with self.assertLogs("demos.model.client", "INFO") as logs:
            await port.aclose()
        self.assertTrue(any("model output gaps" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
