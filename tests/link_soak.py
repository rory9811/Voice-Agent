"""Soak the model link: the real model server, port and host over a 345 ms round trip.

Not collected by default (no ``test_`` prefix) because it runs for about a
minute; run it with ``python3 -m pytest -q tests/link_soak.py``.
"""

import asyncio
import io
import itertools
import statistics
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

import numpy as np
from PIL import Image

from demos.model.adapter import PROTOCOL_TOKENS, RealtimeVenusOmniAdapter
from demos.model.client import RemoteOmniServingPort
from demos.model.wire import StepStats
from harness.bridge.contracts import ModelStep
from harness.bridge.host import VenusOmniServingHost
from harness.bridge.serving import OpenSession
from harness.core.models import AudioChunk, VideoFrame
from link_proxy import LatencyProxy, serve_model

OUTAGE_S = 3.0
ONE_WAY_MS = 172  # half the measured aTrust VPN round trip
# Measured model step times: p50, p95 and the maximum.
STEP_S = (0.4, 0.4, 0.9, 0.4, 1.36)
AUDIO = b"\0\0" * 16_000  # one second of 16 kHz PCM16


def _jpeg() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (16, 16), "gray").save(out, "JPEG")
    return out.getvalue()


JPEG = _jpeg()


class PacedModel:
    """The checkpoint's duplex calls, taking the measured step times and always speaking."""

    def __init__(self) -> None:
        self.official_duplex = self
        self.total_ids: list[int] = []
        self.steps = 0

    def prepare(self, **_) -> None:
        pass

    def streaming_prefill(self, *_) -> None:
        pass

    def streaming_generate(self, **_) -> dict:
        time.sleep(STEP_S[self.steps % len(STEP_S)])
        self.steps += 1
        self.total_ids += [100, 101, 102]  # clear of the protocol token ids
        return {
            "is_listen": False,
            "audio_waveform": np.full(24_000, 0.25, dtype=np.float32),
            "end_of_turn": self.steps % 10 == 0,
        }


def harness_session() -> AsyncMock:
    session = AsyncMock(session_id="soak")
    sequence = itertools.count(1)  # audio and frames share one media sequence
    session.ingest_user_audio.side_effect = session.ingest_user_video_frame.side_effect = (
        lambda _: next(sequence)
    )
    session.consume_total_ids.return_value = ModelStep("x", (), False, 1)
    session.finish_model_turn.return_value = ""
    return session


def speech_gaps_ms(outputs) -> list[tuple[float, int]]:
    """(arrival gap, emission gap) of consecutive audio steps of the same generation."""
    speech = [(at, step) for at, step in outputs if step.audio is not None]
    return [
        (later - earlier, b.at_ms - a.at_ms)
        for (earlier, a), (later, b) in zip(speech, speech[1:])
        if a.generation_id == b.generation_id
    ]


class LinkSoakTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        adapter = RealtimeVenusOmniAdapter(
            PacedModel(),
            special_token_ids={token: index for index, token in enumerate(PROTOCOL_TOKENS)},
            model_revision="soak",
        )
        server = serve_model(adapter)
        url = await server.__aenter__()
        self.addAsyncCleanup(server.__aexit__, None, None, None)
        self.proxy = LatencyProxy(int(url.rsplit(":", 1)[1]), one_way_ms=ONE_WAY_MS)
        proxied = f"http://127.0.0.1:{await self.proxy.start()}"
        self.addAsyncCleanup(self.proxy.close)
        port = RemoteOmniServingPort(proxied, timeout_s=10.0)
        self.addAsyncCleanup(port.aclose)
        self.link_changes = []

        async def link_changed(up):
            self.link_changes.append(up)

        port.on_link_change = link_changed
        opened = await port.open_session(OpenSession(session_id="soak", model="Realtime-Venus-Omni"))
        self.host = VenusOmniServingHost(
            session=harness_session(),
            serving=port,
            playback=MagicMock(enqueue=AsyncMock()),
            opened=opened,
        )
        self.addAsyncCleanup(self.host.close)

    async def run_camera(self, seconds, *, drop_at=None):
        """Send a frame and a second of audio each second; (arrival ms, step) of every output.

        At second ``drop_at`` the proxy kills the link and refuses it for
        ``OUTAGE_S``; the time of the drop is ``self.dropped``.
        """
        loop = asyncio.get_running_loop()
        start = loop.time()
        outputs = []

        def now_ms():
            # Wall clock, like the server's at_ms: both run on this machine, so they compare.
            return time.time() * 1000

        async def send():
            for k in range(seconds):
                await asyncio.sleep(start + k - loop.time())
                if k == drop_at:
                    self.dropped = now_ms()
                    self.proxy.drop_all()
                    self.proxy.refuse_for(OUTAGE_S)
                await self.host.append_video_frame(VideoFrame(JPEG, captured_at_ms=k * 1000))
                await self.host.append_audio(
                    AudioChunk(AUDIO, start_ms=k * 1000, end_ms=(k + 1) * 1000)
                )

        async def receive():
            while (left := start + seconds - loop.time()) > 0:
                try:
                    processed = await self.host.next_output(timeout_s=left)
                except TimeoutError:
                    return
                outputs.append((now_ms(), processed.output))

        await asyncio.gather(send(), receive())
        return outputs

    async def test_camera_pace_over_a_345_ms_link(self):
        # The gaps themselves follow the paced steps (1 s + this step - the last
        # one), so assert only what the link adds: delay, jitter and backlog.
        seconds = 30
        outputs = await self.run_camera(seconds)
        transit = StepStats()
        for at, step in outputs:
            transit.add(at - step.at_ms)
        gaps = speech_gaps_ms(outputs)
        mean_gap = statistics.fmean(arrival for arrival, _ in gaps)
        jitter = max(abs(arrival - emitted) for arrival, emitted in gaps)
        print(
            f"\ntransit {transit.summary()}; {len(gaps)} speech gaps, mean {mean_gap:.0f} ms, "
            f"max jitter {jitter:.1f} ms; {len(outputs)} outputs for {seconds} inputs"
        )
        self.assertLessEqual(transit.summary()["p95_ms"], ONE_WAY_MS + 50)
        self.assertLessEqual(jitter, 50)
        self.assertTrue(900 <= mean_gap <= 1100, mean_gap)
        self.assertGreaterEqual(len(outputs), seconds - 3)
        self.assertIsNone(self.host._poisoned)

    async def test_link_drop_mid_conversation_resumes(self):
        outputs = await self.run_camera(int(10 + OUTAGE_S + 8), drop_at=10)
        # Any lost or repeated event would have poisoned the host.
        self.assertIsNone(self.host._poisoned)
        refused_until = self.dropped + OUTAGE_S * 1000
        resumed = next((at for at, _ in outputs if at > refused_until), float("inf"))
        resumed_s = (resumed - self.dropped) / 1000
        print(f"\noutputs resumed {resumed_s:.2f} s after the drop; {len(outputs)} outputs")
        # Longest backoff 2 s, the slowest step 1.36 s, a round trip and margin.
        self.assertLessEqual(resumed_s, OUTAGE_S + 4)
        self.assertEqual(self.link_changes, [False, True])


if __name__ == "__main__":
    unittest.main()
