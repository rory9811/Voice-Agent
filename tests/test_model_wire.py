import json
import unittest

from demos.model.wire import (
    MAX_FRAME_BYTES,
    FrameError,
    Inbox,
    Outbox,
    decode_frame,
    encode_frame,
)


class FrameTests(unittest.TestCase):
    def test_frame_round_trip_keeps_raw_payload(self):
        header = {"type": "audio", "seq": 1, "ack": 0}
        payload = b"\x01\x02" * 8000
        frame = encode_frame(header, payload)
        self.assertEqual(decode_frame(frame), (header, payload))
        header_len = len(json.dumps(header, separators=(",", ":")).encode())
        self.assertEqual(len(frame), 4 + header_len + 16000)

    def test_decode_rejects_bad_frames(self):
        def raw(header: bytes) -> bytes:
            return len(header).to_bytes(4, "big") + header

        bad = {
            "short": b"\x00\x00",
            "header length out of range": (100).to_bytes(4, "big") + b"x" * 10,
            "header not an object": raw(b"[]"),
            "no type": raw(b'{"seq":1}'),
            "type not a str": raw(b'{"type":1}'),
            "too large": encode_frame({"type": "a"}, b"\0" * MAX_FRAME_BYTES),
        }
        for name, data in bad.items():
            with self.subTest(name), self.assertRaises(FrameError):
                decode_frame(data)


class OutboxTests(unittest.TestCase):
    def test_outbox_keeps_frames_until_confirmed(self):
        box = Outbox()
        self.assertEqual([box.push({"type": "a"}) for _ in range(3)], [1, 2, 3])
        box.confirm(1)
        self.assertEqual([h["seq"] for h, _ in box.after(0)], [2, 3])
        self.assertEqual([h["seq"] for h, _ in box.after(2)], [3])
        box.confirm(3)
        self.assertEqual(box.after(0), [])
        self.assertEqual(box.last_seq, 3)

    def test_outbox_limit(self):
        box = Outbox(max_bytes=10)
        box.push({"type": "a"}, b"x" * 8)
        with self.assertRaises(OverflowError):
            box.push({"type": "a"}, b"x" * 8)


class InboxTests(unittest.TestCase):
    def test_inbox_dedups_and_rejects_gaps(self):
        inbox = Inbox()
        self.assertTrue(inbox.accept(1))
        self.assertFalse(inbox.accept(1))
        self.assertTrue(inbox.accept(2))
        with self.assertRaises(FrameError):
            inbox.accept(4)
        self.assertEqual(inbox.last, 2)
