"""JSON wire protocol between the FastAPI model server and the harness client.

This module is the ONLY shared contract the server and the remote client rely
on.  It deliberately imports neither the Venus harness nor ``harness.bridge`` so the
server can run on a Python 3.10 model environment that cannot import the
harness package (which requires Python 3.11, e.g. ``enum.StrEnum``).

Wire rules (must mirror the frozen dataclasses in ``harness/bridge/serving.py``):

* every DTO is a JSON object whose keys are the dataclass field names verbatim;
* ``bytes`` fields (audio PCM, encoded frames/segments) become base64 strings;
* ``tuple[int, ...]`` (token IDs) become JSON arrays of ints;
* ``Mapping[str, int]`` (special_token_ids) becomes a JSON object token->id;
* enums become their ``.value`` strings;
* ``model_revision`` / identifiers are plain strings; ``*_ms`` fields are ints.

The server (py3.10) emits/parses plain dicts with these helpers.  The harness
client (py3.11) reconstructs the real DTOs from the same dicts.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
from collections import deque
from typing import Any

# Mirror harness/bridge/serving.py:24
PROTOCOL_VERSION = "realtime-venus-harness/3"
MAX_FRAME_BYTES = 1 << 20

# Echo harness/bridge/serving.py enum .value strings.  The server uses DELTA mode:
# each step carries only the token IDs generated since the previous step, so
# the session-wide ``total_ids`` accumulation does not collide with the
# harness per-generation append-only fence.
RAW_TOKEN_MODE = "delta"
FORMAT_PCM_S16LE = "pcm_s16le"
VISIBILITY_PRIVATE = "private"


def now_ms() -> int:
    """Monotonic-ish wall clock in milliseconds for *_ms fence fields."""

    import time

    return time.time_ns() // 1_000_000


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(value: str) -> bytes:
    return base64.b64decode(value.encode("ascii"))


# ---------------------------------------------------------------------------
# Audio conversions.  Model side only: input is 16 kHz PCM s16le from the
# harness; output native speech is 24 kHz float32 numpy.
# ---------------------------------------------------------------------------


def pcm16le_to_float32(pcm: bytes) -> "Any":
    """16-bit signed little-endian PCM bytes -> float32 numpy in [-1, 1]."""

    import numpy as np

    if not pcm:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def float32_to_pcm16le(wave: "Any") -> bytes:
    """float32 numpy in [-1, 1] -> 16-bit signed little-endian PCM bytes."""

    import numpy as np

    if wave is None:
        return b""
    arr = np.asarray(wave, dtype=np.float32)
    clipped = np.clip(arr, -1.0, 1.0)
    return (clipped * 32768.0).round().astype("<i2").tobytes()


def decode_frames(frame_bytes_list: Any) -> list[Any]:
    """Decode transmitted JPEG/PNG frames to the RGB images required by the model."""
    import io

    from PIL import Image

    frames = []
    for data in frame_bytes_list:
        with Image.open(io.BytesIO(data)) as image:
            frames.append(image.convert("RGB"))
    return frames


def generated_audio_dict(
    wave: "Any", sample_rate_hz: int = 24_000
) -> dict[str, Any] | None:
    """Build the GeneratedAudio wire dict from a float32 waveform.

    Returns None when the model produced no audio (e.g. a ``listen`` chunk or a
    suppressed delegate step): the harness ``ModelOutputStep`` allows
    ``audio=None`` as long as the step otherwise carries tokens or turn finish.
    """

    if wave is None:
        return None
    data = float32_to_pcm16le(wave)
    if not data:
        return None
    return {
        "data": b64(data),
        "sample_rate_hz": int(sample_rate_hz),
        "channels": 1,
        "sample_width_bytes": 2,
        "format": FORMAT_PCM_S16LE,
    }


def parse_audio_append(req: dict[str, Any]) -> tuple[bytes, int]:
    """Return (raw_pcm_bytes, sample_rate_hz) from an AudioAppend wire dict."""

    data = unb64(req["data"])
    sample_rate = int(req.get("sample_rate_hz", 16_000))
    return data, sample_rate


def parse_frame_append(req: dict[str, Any]) -> tuple[bytes, str]:
    """Return (image_bytes, mime_type) from a VideoFrameAppend wire dict."""

    return unb64(req["data"]), str(req.get("mime_type", "image/jpeg"))


# ---------------------------------------------------------------------------
# Wire dict builders for server responses.  Field names mirror the DTOs.
# ---------------------------------------------------------------------------


def session_opened(
    *,
    session_id: str,
    incarnation: int,
    model: str,
    model_revision: str,
    special_token_ids: dict[str, int],
    opened_at_ms: int,
    video_segments: bool = False,
    max_sessions: int = 1,
    delegate_audio_filtered: bool = False,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "incarnation": incarnation,
        "model": model,
        "model_revision": model_revision,
        "capabilities": {
            "special_token_ids": dict(special_token_ids),
            "raw_token_mode": RAW_TOKEN_MODE,
            "same_kv_prefill": True,
            "duplex_media": True,
            "ordered_input_fence": True,
            "native_audio_output": True,
            "video_segments": bool(video_segments),
            "max_sessions": int(max_sessions),
            "delegate_audio_filtered": bool(delegate_audio_filtered),
        },
        "opened_at_ms": int(opened_at_ms),
    }


def input_accepted(
    *,
    session_id: str,
    incarnation: int,
    event_seq: int,
    input_seq: int,
    accepted_at_ms: int,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "incarnation": incarnation,
        "event_seq": int(event_seq),
        "input_seq": int(input_seq),
        "accepted_at_ms": int(accepted_at_ms),
    }


def model_output_step(
    *,
    session_id: str,
    incarnation: int,
    generation_id: str,
    generation_epoch: int,
    step_seq: int,
    input_seq_cutoff: int,
    at_ms: int,
    total_token_ids: tuple[int, ...],
    audio: dict[str, Any] | None,
    audio_chunk_seq: int | None,
    turn_finished: bool,
    finish_reason: str | None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "incarnation": incarnation,
        "generation_id": generation_id,
        "generation_epoch": int(generation_epoch),
        "step_seq": int(step_seq),
        "input_seq_cutoff": int(input_seq_cutoff),
        "at_ms": int(at_ms),
        "total_token_ids": [int(x) for x in total_token_ids],
        "audio": audio,
        "audio_chunk_seq": None if audio_chunk_seq is None else int(audio_chunk_seq),
        "turn_finished": bool(turn_finished),
        "finish_reason": finish_reason,
    }


def prefill_applied(
    *,
    session_id: str,
    incarnation: int,
    work_id: str,
    attempt_id: str,
    generation_id: str,
    generation_epoch: int,
    kv_position: int,
    applied_at_ms: int,
    deduplicated: bool = False,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "incarnation": incarnation,
        "work_id": work_id,
        "attempt_id": attempt_id,
        "generation_id": generation_id,
        "generation_epoch": int(generation_epoch),
        "kv_position": int(kv_position),
        "applied_at_ms": int(applied_at_ms),
        "deduplicated": bool(deduplicated),
    }


def playback_accepted(
    *,
    session_id: str,
    incarnation: int,
    utterance_id: str,
    cumulative_played_chunks: int,
    accepted_at_ms: int,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "incarnation": incarnation,
        "utterance_id": utterance_id,
        "cumulative_played_chunks": int(cumulative_played_chunks),
        "accepted_at_ms": int(accepted_at_ms),
    }


def session_closed(
    *, session_id: str, incarnation: int, closed_at_ms: int
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "incarnation": incarnation,
        "closed_at_ms": int(closed_at_ms),
    }


class FrameError(ValueError):
    """A received frame is malformed or breaks the sequence."""


def encode_frame(header: dict[str, Any], payload: bytes = b"") -> bytes:
    """4-byte big-endian header length, UTF-8 JSON header, raw payload (no base64)."""

    head = json.dumps(header, separators=(",", ":")).encode()
    return len(head).to_bytes(4, "big") + head + payload


def decode_frame(data: bytes) -> tuple[dict[str, Any], bytes]:
    if len(data) > MAX_FRAME_BYTES:
        raise FrameError(f"frame of {len(data)} bytes exceeds {MAX_FRAME_BYTES}")
    if len(data) < 4:
        raise FrameError("frame shorter than its length prefix")
    end = 4 + int.from_bytes(data[:4], "big")
    if end > len(data):
        raise FrameError("header length exceeds frame")
    try:
        header = json.loads(data[4:end])
    except ValueError as exc:
        raise FrameError(f"header is not JSON: {exc}") from exc
    if not isinstance(header, dict) or not isinstance(header.get("type"), str):
        raise FrameError("header must be an object with a string type")
    return header, data[end:]


class Outbox:
    """Sent frames kept until the peer confirms them, so a reconnect can replay them."""

    def __init__(self, max_bytes: int = 32 << 20) -> None:
        self._max_bytes = max_bytes
        self._frames: deque[tuple[dict[str, Any], bytes]] = deque()
        self._bytes = 0
        self.last_seq = 0
        # Wakes the writer coroutine; it clears the event once it has drained.
        self.ready = asyncio.Event()

    def push(self, header: dict[str, Any], payload: bytes = b"") -> int:
        if self._bytes + len(payload) > self._max_bytes:
            raise OverflowError("unconfirmed frames exceed the outbox limit")
        self.last_seq += 1
        header["seq"] = self.last_seq
        self._frames.append((header, payload))
        self._bytes += len(payload)
        self.ready.set()
        return self.last_seq

    def confirm(self, ack: int) -> None:
        while self._frames and self._frames[0][0]["seq"] <= ack:
            self._bytes -= len(self._frames.popleft()[1])

    def after(self, seq: int) -> list[tuple[dict[str, Any], bytes]]:
        return [f for f in self._frames if f[0]["seq"] > seq]


class Inbox:
    """Accepts frames strictly in order; a replay after reconnect shows up as a duplicate."""

    def __init__(self) -> None:
        self.last = 0

    def accept(self, seq: int) -> bool:
        if seq <= self.last:
            return False
        if seq > self.last + 1:
            raise FrameError(f"sequence gap: expected {self.last + 1}, got {seq}")
        self.last = seq
        return True


class StepStats:
    """Wall-clock time of recent model steps (one step per second of input)."""

    def __init__(self, size: int = 600) -> None:
        self._ms: deque[float] = deque(maxlen=size)

    def add(self, ms: float) -> None:
        self._ms.append(ms)

    def summary(self) -> dict[str, Any]:
        if not self._ms:
            return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
        ordered = sorted(self._ms)

        def nearest_rank(p: float) -> float:
            return round(ordered[math.ceil(p * len(ordered)) - 1], 1)

        return {
            "count": len(ordered),
            "p50_ms": nearest_rank(0.5),
            "p95_ms": nearest_rank(0.95),
            "max_ms": round(ordered[-1], 1),
        }


__all__ = [
    "PROTOCOL_VERSION",
    "MAX_FRAME_BYTES",
    "FrameError",
    "encode_frame",
    "decode_frame",
    "Outbox",
    "Inbox",
    "StepStats",
    "RAW_TOKEN_MODE",
    "FORMAT_PCM_S16LE",
    "VISIBILITY_PRIVATE",
    "now_ms",
    "b64",
    "unb64",
    "pcm16le_to_float32",
    "float32_to_pcm16le",
    "generated_audio_dict",
    "parse_audio_append",
    "parse_frame_append",
    "session_opened",
    "input_accepted",
    "model_output_step",
    "prefill_applied",
    "playback_accepted",
    "session_closed",
]
