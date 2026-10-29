import asyncio
import threading
import unittest
from unittest.mock import patch

import numpy as np

from demos.model.adapter import AdapterError, ModelMismatch, SessionBusy, SessionLost
from demos.model.wire import PROTOCOL_VERSION
from test_model_server import omni_adapter

SPEAK = {"is_listen": False, "end_of_turn": False, "audio_waveform": np.zeros(240, dtype=np.float32)}
END = {"is_listen": False, "end_of_turn": True, "audio_waveform": None}


def open_request(session_id="A", model="Realtime-Venus-Omni"):
    return {"session_id": session_id, "model": model, "protocol_version": PROTOCOL_VERSION}


async def until(predicate, timeout_s=2.0):
    for _ in range(int(timeout_s / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached in time")


class DriverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = omni_adapter()
        self.link = asyncio.Event()
        self.sess, _ = await self.adapter.open_session(open_request(), self.link)
        self.seq = 0
        self.event_seq = 0

    async def asyncTearDown(self):
        await self.adapter.close_session(self.sess, "test_end")

    def send(self, kind, payload=b"", **fields):
        self.seq += 1
        header = {"type": kind, "seq": self.seq, "ack": 0, "consumed": 0, **fields}
        self.adapter.receive(self.sess, header, payload)

    def audio(self, seconds=1.0):
        self.event_seq += 1
        pcm = b"\0\0" * int(16_000 * seconds)
        self.send("audio", pcm, event_seq=self.event_seq, sample_rate_hz=16_000)

    def prefill(self, attempt_id="attempt-1", text="backend result"):
        self.send("prefill", work_id="work-1", attempt_id=attempt_id, text_list=[text])

    def sent(self, kind=None):
        return [h for h, _ in self.sess.outbox.after(0) if kind is None or h["type"] == kind]

    async def test_append_is_acknowledged_during_a_running_step(self):
        entered, gate = threading.Event(), threading.Event()
        self.adapter._official.set_session_stop.side_effect = gate.set

        def generate(**_):
            entered.set()
            gate.wait(5)
            return SPEAK

        self.adapter._model.streaming_generate.side_effect = generate
        self.audio()
        await until(entered.is_set)
        self.audio()
        self.assertEqual([h["event_seq"] for h in self.sent("input_accepted")], [1, 2])
        self.assertEqual(self.sent("output"), [])

    async def test_steps_without_any_pull(self):
        self.audio()
        await until(lambda: self.sent("output"), timeout_s=1.0)

    async def test_ack_precedes_the_output_that_consumes_it(self):
        self.audio()
        await until(lambda: self.sent("output"))
        frames = self.sent()
        ack = next(i for i, h in enumerate(frames) if h["type"] == "input_accepted" and h["input_seq"] == 1)
        output = next(i for i, h in enumerate(frames) if h["type"] == "output" and h["input_seq_cutoff"] == 1)
        self.assertLess(ack, output)

    async def test_output_window_bounds_unconsumed_steps(self):
        self.adapter._model.streaming_generate.return_value = SPEAK
        self.audio(seconds=10)
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.sent("output")), 4)
        self.adapter.receive(self.sess, {"type": "state", "ack": 0, "consumed": 2}, b"")
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.sent("output")), 6)

    async def test_prefill_waits_for_the_generation_boundary(self):
        prefill_sent = threading.Event()
        calls = []

        def foreground(**_):
            calls.append(1)
            if len(calls) == 2:
                prefill_sent.wait(5)  # the prefill arrives while the generation is running
            return END if len(calls) == 3 else SPEAK

        self.adapter._model.streaming_generate.side_effect = foreground
        self.adapter._official.streaming_generate.return_value = END
        self.audio(seconds=3)
        await until(lambda: self.sent("output"))
        self.prefill()
        prefill_sent.set()
        await until(lambda: any(h["generation_id"].startswith("backend-") for h in self.sent("output")))
        frames = self.sent()
        applied = next(i for i, h in enumerate(frames) if h["type"] == "prefill_applied")
        finished = next(i for i, h in enumerate(frames) if h["type"] == "output" and h["turn_finished"])
        backend = [i for i, h in enumerate(frames)
                   if h["type"] == "output" and h["generation_id"].startswith("backend-")]
        self.assertTrue(frames[finished]["generation_id"].startswith("foreground-"))
        self.assertLess(finished, applied)
        self.assertLess(applied, min(backend))

    async def test_prefill_is_idempotent_by_attempt_id(self):
        self.prefill()
        await until(lambda: self.sent("prefill_applied"))
        self.prefill()
        first, second = self.sent("prefill_applied")
        self.assertFalse(first["deduplicated"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(second["generation_id"], first["generation_id"])
        backend_writes = [c for c in self.adapter._official.streaming_prefill.call_args_list
                          if c.args[2] == ["backend result"]]
        self.assertEqual(len(backend_writes), 1)

    async def test_playback_ack_moving_backwards_is_rejected(self):
        self.send("playback_ack", utterance_id="u1", cumulative_played_chunks=2)
        with self.assertRaises(AdapterError):
            self.send("playback_ack", utterance_id="u1", cumulative_played_chunks=1)

    async def test_counters_must_be_integers(self):
        for field, value in [("seq", "1"), ("ack", True), ("consumed", None)]:
            header = {"type": "audio", "seq": 1, "ack": 0, "consumed": 0, "event_seq": 1,
                      "sample_rate_hz": 16_000, field: value}
            with self.subTest(field=field), self.assertRaises(AdapterError):
                self.adapter.receive(self.sess, header, b"\0\0")

    async def test_replayed_frame_is_ignored(self):
        self.audio()
        header = {"type": "audio", "seq": 1, "ack": 0, "consumed": 0, "event_seq": 1, "sample_rate_hz": 16_000}
        self.adapter.receive(self.sess, header, b"\0\0")
        self.assertEqual(len(self.sent("input_accepted")), 1)

    async def test_close_frame_closes_the_session(self):
        self.send("close", reason="client_close")
        await asyncio.wait_for(self.link.wait(), 2)
        self.assertTrue(self.sess.closed)
        self.assertEqual(self.sent()[-1]["type"], "closed")

    async def test_model_error_closes_without_a_closed_frame(self):
        self.adapter._model.streaming_generate.side_effect = RuntimeError("boom")
        with self.assertLogs("demos.model.adapter", "ERROR"):
            self.audio()
            await asyncio.wait_for(self.link.wait(), 2)
        self.assertTrue(self.sess.closed)
        self.assertEqual(self.sess.failure, "RuntimeError: boom")
        self.assertEqual(self.sent("closed"), [])


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.adapter = omni_adapter()

    async def asyncTearDown(self):
        session = self.adapter._session
        if session is not None:
            await self.adapter.close_session(session, "test_end")

    async def open(self, session_id="A", model="Realtime-Venus-Omni"):
        link = asyncio.Event()
        sess, opened = await self.adapter.open_session(open_request(session_id, model), link)
        self.assertEqual(opened["type"], "opened")
        return sess, link

    async def test_open_rejects_other_model(self):
        with self.assertRaises(ModelMismatch):
            await self.open(model="Realtime-Venus-Audio")

    async def test_open_refuses_an_attached_session(self):
        await self.open("A")
        with self.assertRaises(SessionBusy):
            await self.open("B")

    async def test_open_preempts_a_detached_session(self):
        a, link = await self.open("A")
        self.adapter.detach(a, link)
        b, _ = await self.open("B")
        self.assertTrue(a.closed)
        self.assertEqual(b.incarnation, a.incarnation + 1)

    async def test_detached_session_expires(self):
        sess, link = await self.open()
        with patch("demos.model.adapter.RESUME_WINDOW_S", 0.1):
            self.adapter.detach(sess, link)
            await asyncio.sleep(0.3)
        self.assertTrue(sess.closed)

    async def test_resume_reattaches_and_supersedes(self):
        sess, link = await self.open()
        with patch("demos.model.adapter.RESUME_WINDOW_S", 0.1):
            self.adapter.detach(sess, link)
            second = asyncio.Event()
            resumed = await self.adapter.resume_session(
                "A", sess.incarnation, ack=0, consumed=0, link=second)
            await asyncio.sleep(0.3)
        self.assertIs(resumed, sess)
        self.assertFalse(sess.closed)
        await self.adapter.resume_session("A", sess.incarnation, ack=0, consumed=0, link=asyncio.Event())
        self.assertTrue(second.is_set())

    async def test_resume_rejects_unknown_session(self):
        sess, _ = await self.open()
        for session_id, incarnation in [("B", sess.incarnation), ("A", sess.incarnation + 1)]:
            with self.subTest(session_id=session_id, incarnation=incarnation), self.assertRaises(SessionLost):
                await self.adapter.resume_session(
                    session_id, incarnation, ack=0, consumed=0, link=asyncio.Event())
        await self.adapter.close_session(sess, "client_close")
        with self.assertRaises(SessionLost):
            await self.adapter.resume_session("A", sess.incarnation, ack=0, consumed=0, link=asyncio.Event())


if __name__ == "__main__":
    unittest.main()
