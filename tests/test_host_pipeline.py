import asyncio
import itertools
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from harness.bridge.contracts import BackendInjection, ModelStep
from harness.bridge.host import HostProtocolError, VenusOmniServingHost
from harness.bridge.serving import (
    AudioAppend,
    GeneratedAudio,
    InputAccepted,
    ModelOutputStep,
    PlaybackAck,
    PrefillAppend,
    PrefillApplied,
    RawTokenMode,
    SessionCapabilities,
    SessionClosed,
    SessionOpened,
)
from harness.core.models import AudioChunk
from harness.core.venus import VENUS_PROTOCOL_TOKENS

OPENED = SessionOpened(
    session_id="s",
    incarnation=1,
    model="Realtime-Venus-Omni",
    model_revision="r",
    capabilities=SessionCapabilities(
        special_token_ids={t: i for i, t in enumerate(VENUS_PROTOCOL_TOKENS)},
        raw_token_mode=RawTokenMode.DELTA,
    ),
    opened_at_ms=0,
)
CHUNK = AudioChunk(data=b"\0\0", start_ms=0, end_ms=1)
INJECTION = BackendInjection(
    work_id="w1",
    text="<backend>done</backend>",
    result=SimpleNamespace(feedback_id="f1", delivery_id="f1", status="completed"),
)


class FakeServing:
    def __init__(self):
        self.sent = []
        self.events = asyncio.Queue()

    def send_audio(self, request):
        self.sent.append(request)

    send_video_frame = send_video_segment = send_prefill = send_playback_ack = send_audio

    async def next_event(self, *, timeout_s=None):
        event = await asyncio.wait_for(self.events.get(), timeout_s)
        if isinstance(event, BaseException):
            raise event
        return event

    async def close_session(self, request):
        self.events.put_nowait(RuntimeError("closed"))
        return SessionClosed(
            session_id=request.session_id,
            incarnation=request.incarnation,
            closed_at_ms=0,
        )


def ack(event_seq, input_seq):
    return InputAccepted(
        session_id="s",
        incarnation=1,
        event_seq=event_seq,
        input_seq=input_seq,
        accepted_at_ms=0,
    )


def step(generation_id, epoch, *, cutoff=0, finished=False, audio=False):
    return ModelOutputStep(
        session_id="s",
        incarnation=1,
        generation_id=generation_id,
        generation_epoch=epoch,
        step_seq=1,
        input_seq_cutoff=cutoff,
        at_ms=0,
        total_token_ids=(100,),
        audio=GeneratedAudio(b"\0\0") if audio else None,
        audio_chunk_seq=1 if audio else None,
        turn_finished=finished,
        finish_reason="turn_eos" if finished else None,
    )


class HostPipelineTests(unittest.IsolatedAsyncioTestCase):
    def make_host(self, *, feedback_timeout_s=60.0):
        session = MagicMock(session_id="s", feedback_timeout_s=feedback_timeout_s)
        session.ingest_user_audio = AsyncMock(side_effect=itertools.count(1))
        session.consume_total_ids = AsyncMock(return_value=ModelStep("hi", (), False, 1))
        session.finish_model_turn = AsyncMock(return_value="")
        session.append_playback_chunk = AsyncMock()
        session.acknowledge_playback_chunks = AsyncMock(return_value="hi")
        session.results = asyncio.Queue()
        session.next_backend_injection = session.results.get
        session.injection_is_current = MagicMock(return_value=True)
        session.discard_injection = AsyncMock()
        session.close = AsyncMock()
        fake = FakeServing()
        host = VenusOmniServingHost(
            session=session,
            serving=fake,
            playback=MagicMock(enqueue=AsyncMock()),
            opened=OPENED,
        )
        host.start_backend_collector()
        self.addAsyncCleanup(host.close)
        return host, fake, session

    async def queue_result(self, session):
        session.results.put_nowait(INJECTION)
        await asyncio.sleep(0.01)  # let the collector move it into the host

    async def test_appends_do_not_wait_for_acks(self):
        host, fake, _ = self.make_host()
        await asyncio.wait_for(host.append_audio(CHUNK), 0.5)
        await asyncio.wait_for(host.append_audio(CHUNK), 0.5)
        self.assertTrue(fake.events.empty())
        self.assertEqual([type(r) for r in fake.sent], [AudioAppend, AudioAppend])
        self.assertEqual([r.event_seq for r in fake.sent], [1, 2])

    async def test_acks_are_matched_in_order(self):
        host, fake, _ = self.make_host()
        await host.append_audio(CHUNK)
        await host.append_audio(CHUNK)
        output = step("foreground-s:1", 1, cutoff=2)
        for event in (ack(1, 1), ack(2, 2), output):
            fake.events.put_nowait(event)
        processed = await host.next_output(timeout_s=1)
        self.assertIs(processed.output, output)

    async def test_out_of_order_ack_poisons(self):
        host, fake, _ = self.make_host()
        await host.append_audio(CHUNK)
        await host.append_audio(CHUNK)
        fake.events.put_nowait(ack(2, 1))
        with self.assertRaises(HostProtocolError):
            await host.next_output(timeout_s=1)
        with self.assertRaisesRegex(RuntimeError, "failed state"):
            await host.append_audio(CHUNK)

    async def test_output_cannot_cite_unacked_input(self):
        host, fake, _ = self.make_host()
        await host.append_audio(CHUNK)
        fake.events.put_nowait(step("foreground-s:1", 1, cutoff=1))
        with self.assertRaises(HostProtocolError):
            await host.next_output(timeout_s=1)

    async def start_backend_generation(self, host, fake, session):
        await self.queue_result(session)
        self.assertTrue(await host.begin_backend_turn())
        prefill = fake.sent[-1]
        self.assertIsInstance(prefill, PrefillAppend)
        self.assertFalse(await host.begin_backend_turn())
        fake.events.put_nowait(step("foreground-s:1", 1, finished=True))
        fake.events.put_nowait(
            PrefillApplied(
                session_id="s",
                incarnation=1,
                work_id="w1",
                attempt_id=prefill.attempt_id,
                generation_id="backend-s:2",
                generation_epoch=2,
                kv_position=10,
                applied_at_ms=0,
            )
        )
        fake.events.put_nowait(step("backend-s:2", 2, audio=True))
        foreground = await host.next_output(timeout_s=1)
        backend = await host.next_output(timeout_s=1)
        return foreground, backend

    async def test_prefill_applies_in_stream_order(self):
        host, fake, session = self.make_host()
        foreground, backend = await self.start_backend_generation(host, fake, session)
        self.assertEqual(foreground.caused_by_work_id, "")
        self.assertEqual(backend.caused_by_work_id, "w1")

    async def test_unanswered_prefill_times_out(self):
        host, _, session = self.make_host(feedback_timeout_s=0.2)
        await self.queue_result(session)
        self.assertTrue(await host.begin_backend_turn())
        await asyncio.sleep(0.6)
        session.feedback_failed.assert_called_with(INJECTION)
        with self.assertRaisesRegex(RuntimeError, "failed state"):
            await host.append_audio(CHUNK)

    async def test_playback_ack_sent_without_waiting(self):
        host, fake, session = self.make_host()
        _, backend = await self.start_backend_generation(host, fake, session)
        await asyncio.wait_for(host.acknowledge_playback(backend.utterance_id, 1), 0.5)
        self.assertIsInstance(fake.sent[-1], PlaybackAck)
        self.assertEqual(fake.sent[-1].cumulative_played_chunks, 1)

    async def test_close_unblocks_next_event(self):
        host, _, _ = self.make_host()
        pending = asyncio.create_task(host.next_output())
        await asyncio.sleep(0.01)
        await host.close()
        done, _ = await asyncio.wait({pending}, timeout=1)
        self.assertIn(pending, done)
        self.assertIsInstance(pending.exception(), RuntimeError)


if __name__ == "__main__":
    unittest.main()
