"""Bounded, timestamped raw-media buffers and played-TTS accounting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, List, Optional, Tuple, TypeVar

from .models import AudioChunk, VideoFrame, VideoSegment

T = TypeVar("T")


@dataclass(frozen=True)
class _Record(Generic[T]):
    sequence: int
    at_ms: int
    value: T
    size_bytes: int


class TimedRingBuffer(Generic[T]):
    """Bound retention by time and bytes without persisting raw media."""

    def __init__(self, retention_ms: int, max_bytes: int) -> None:
        self._retention_ms = retention_ms
        self._max_bytes = max_bytes
        self._items: List[_Record[T]] = []
        self._bytes = 0

    def append(self, sequence: int, at_ms: int, value: T, size_bytes: int) -> None:
        self._items.append(_Record(sequence, at_ms, value, size_bytes))
        self._bytes += size_bytes
        self.prune(at_ms)

    def prune(self, now_ms: int) -> None:
        minimum_ms = now_ms - self._retention_ms
        while self._items and (
            self._items[0].at_ms < minimum_ms or self._bytes > self._max_bytes
        ):
            removed = self._items.pop(0)
            self._bytes -= removed.size_bytes

    def between(
        self,
        start_ms: int,
        end_ms: int,
        cutoff_sequence: int,
    ) -> Tuple[T, ...]:
        return tuple(
            record.value
            for record in self._items
            if (
                record.sequence <= cutoff_sequence
                and start_ms <= record.at_ms <= end_ms
            )
        )

    def latest_before(self, end_ms: int, cutoff_sequence: int) -> Optional[T]:
        for record in reversed(self._items):
            if record.sequence <= cutoff_sequence and record.at_ms <= end_ms:
                return record.value
        return None

    def values_before(self, cutoff_sequence: int) -> Tuple[T, ...]:
        """Return retained values that existed at a sequence boundary."""

        return tuple(
            record.value for record in self._items if record.sequence <= cutoff_sequence
        )

    def trim_to_count(self, maximum: int) -> None:
        """Keep the newest values when a count bound complements byte bounds."""

        if maximum < 0:
            raise ValueError("maximum must not be negative")
        if len(self._items) <= maximum:
            return
        self._items = [] if maximum == 0 else self._items[-maximum:]
        self._bytes = sum(item.size_bytes for item in self._items)

    @property
    def size_bytes(self) -> int:
        return self._bytes


@dataclass
class _TtsUtterance:
    text: str = ""
    played_chars: int = 0


class PlayedTtsTracker:
    """Publishes text only after a real playback acknowledgement advances."""

    def __init__(self, max_chars: int = 4_000) -> None:
        self._max_chars = max_chars
        self._utterances: dict[str, _TtsUtterance] = {}
        self._played: List[Tuple[int, str]] = []

    def append_text(self, utterance_id: str, text: str) -> None:
        if not utterance_id:
            raise ValueError("utterance_id is required")
        if not text:
            return
        utterance = self._utterances.setdefault(utterance_id, _TtsUtterance())
        utterance.text += text

    def advance(self, utterance_id: str, played_chars: int, at_ms: int) -> str:
        utterance = self._utterances.get(utterance_id)
        if utterance is None:
            raise ValueError("playback acknowledgement references an unknown utterance")
        bounded = max(utterance.played_chars, min(played_chars, len(utterance.text)))
        newly_played = utterance.text[utterance.played_chars : bounded]
        utterance.played_chars = bounded
        if newly_played:
            self._played.append((at_ms, newly_played))
            self._prune()
        return newly_played

    def complete(self, utterance_id: str, at_ms: int) -> str:
        utterance = self._utterances.get(utterance_id)
        if utterance is None:
            raise ValueError("playback completion references an unknown utterance")
        return self.advance(utterance_id, len(utterance.text), at_ms)

    def recent_text(self, cutoff_ms: int, max_chars: int) -> str:
        combined = "".join(text for at_ms, text in self._played if at_ms <= cutoff_ms)
        return combined[-max_chars:]

    def _prune(self) -> None:
        combined = "".join(text for _, text in self._played)
        overflow = len(combined) - self._max_chars
        while overflow > 0 and self._played:
            at_ms, text = self._played[0]
            if len(text) <= overflow:
                self._played.pop(0)
                overflow -= len(text)
            else:
                self._played[0] = (at_ms, text[overflow:])
                overflow = 0


class SessionBuffers:
    """All volatile evidence for one realtime conversation session."""

    def __init__(
        self,
        *,
        retention_ms: int,
        max_audio_bytes: int,
        max_video_bytes: int,
        max_video_frames: int,
    ) -> None:
        self.audio = TimedRingBuffer[AudioChunk](retention_ms, max_audio_bytes)
        self.video_segments = TimedRingBuffer[VideoSegment](
            retention_ms, max_video_bytes
        )
        self.video_frames = TimedRingBuffer[VideoFrame](retention_ms, max_video_bytes)
        self.played_tts = PlayedTtsTracker()
        self.max_video_frames = max_video_frames

    def add_audio(self, sequence: int, chunk: AudioChunk) -> None:
        self.audio.append(sequence, chunk.end_ms, chunk, chunk.size_bytes)

    def add_video_segment(self, sequence: int, segment: VideoSegment) -> None:
        self.video_segments.append(
            sequence, segment.end_ms, segment, segment.size_bytes
        )

    def add_video_frame(self, sequence: int, frame: VideoFrame) -> None:
        self.video_frames.append(
            sequence, frame.captured_at_ms, frame, frame.size_bytes
        )
        # Byte/time limits are primary. This soft cap makes frame-only
        # integrations predictable even when tiny JPEGs arrive rapidly.
        self.video_frames.trim_to_count(self.max_video_frames)
