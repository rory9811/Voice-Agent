"""Training-free duplex gating and arbitration for completed results."""

from __future__ import annotations

import io
import math
import wave
from array import array
from dataclasses import dataclass
from sys import byteorder
from typing import Dict, List, Optional

from .config import ResultManagementConfig
from .models import AudioChunk, DelegateResult


@dataclass(frozen=True)
class ReadyItem:
    result: DelegateResult
    ready_at_ms: int
    enqueue_order: int
    estimated_speech_ms: int


@dataclass(frozen=True)
class ReleaseDecision:
    result: DelegateResult
    ready_wait_ms: int
    estimated_speech_ms: int
    laxity_ms: int
    backlog_size: int


@dataclass
class _Playback:
    total_chars: int = 0
    played_chars: int = 0


def _pcm16_rms(data: bytes) -> Optional[float]:
    usable = len(data) - (len(data) % 2)
    if usable < 2:
        return None
    samples = array("h")
    samples.frombytes(data[:usable])
    if byteorder != "little":
        samples.byteswap()
    if not samples:
        return None
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def audio_rms(chunk: AudioChunk) -> Optional[float]:
    """Return RMS for PCM16LE/WAV input, or ``None`` for unknown audio."""

    data = chunk.data
    sample_width = chunk.sample_width_bytes
    if chunk.mime_type in {"audio/wav", "audio/x-wav"} or data.startswith(b"RIFF"):
        try:
            with wave.open(io.BytesIO(data), "rb") as reader:
                sample_width = reader.getsampwidth()
                data = reader.readframes(reader.getnframes())
        except (EOFError, wave.Error):
            return None
    if sample_width != 2:
        return None
    return _pcm16_rms(data)


class DuplexResultManager:
    """Session-local result backlog, gap gate, and presentation lock."""

    def __init__(self, config: ResultManagementConfig, *, max_backlog: int) -> None:
        if max_backlog < 1:
            raise ValueError("max_backlog must be positive")
        self._config = config
        self._max_backlog = max_backlog
        self._ready: List[ReadyItem] = []
        self._enqueue_order = 0
        self._user_speaking_until_ms = 0
        self._playback: Dict[str, _Playback] = {}
        self._gap_started_at_ms: Optional[int] = None
        self._queued_work_id: Optional[str] = None
        self._presentation_work_id: Optional[str] = None
        self._presentation_state = "idle"
        self._presentation_started_at_ms = 0
        self._frontend_turn_finished = False

    @property
    def backlog_size(self) -> int:
        return len(self._ready)

    @property
    def has_timed_work(self) -> bool:
        return bool(self._ready) or self._presentation_state != "idle"

    @property
    def presentation_state(self) -> str:
        return self._presentation_state

    def offer(self, result: DelegateResult, now_ms: int) -> Optional[DelegateResult]:
        if result.feedback_id:
            self._ready = [
                item
                for item in self._ready
                if not (
                    item.result.work_id == result.work_id
                    and item.result.status == "pending"
                    and item.result.feedback_id
                )
            ]
        self._enqueue_order += 1
        estimated_ms = max(
            1,
            math.ceil(len(result.spoken_text) * 1_000 / self._config.chars_per_second),
        )
        self._ready.append(
            ReadyItem(
                result=result,
                ready_at_ms=now_ms,
                enqueue_order=self._enqueue_order,
                estimated_speech_ms=estimated_ms,
            ),
        )
        if len(self._ready) <= self._max_backlog:
            return None
        dropped = max(self._ready, key=lambda item: self._scheduling_key(item, now_ms))
        self._ready.remove(dropped)
        return dropped.result

    def observe_user_audio(self, chunk: AudioChunk, now_ms: int) -> float:
        rms = audio_rms(chunk)
        # Unknown encodings are treated conservatively as speech.
        measured = self._config.speech_rms_threshold if rms is None else rms
        if rms is None or rms >= self._config.speech_rms_threshold:
            self._user_speaking_until_ms = max(
                self._user_speaking_until_ms,
                now_ms
                + max(self._config.speech_hold_ms, chunk.end_ms - chunk.start_ms),
            )
            self._gap_started_at_ms = None
        else:
            self._refresh_gap(now_ms)
        return measured

    def observe_frontend_text(self, text: str, now_ms: int) -> None:
        if not text:
            return
        if self._presentation_state in {"waiting_frontend_reaction", "playing"}:
            self._presentation_state = "playing"
            self._presentation_started_at_ms = now_ms
        self._gap_started_at_ms = None

    def observe_tts_text(self, utterance_id: str, text: str, now_ms: int) -> None:
        if not utterance_id:
            raise ValueError("utterance_id is required")
        if not text:
            return
        playback = self._playback.setdefault(utterance_id, _Playback())
        playback.total_chars += len(text)
        self.observe_frontend_text(text, now_ms)

    def acknowledge_playback(
        self, utterance_id: str, played_chars: int, now_ms: int
    ) -> None:
        playback = self._playback.get(utterance_id)
        if playback is None:
            return
        playback.played_chars = max(
            playback.played_chars,
            min(played_chars, playback.total_chars),
        )
        if (
            self._presentation_state == "playing"
            and self._frontend_turn_finished
            and not self._playback_active
        ):
            self._clear_presentation()
        self._refresh_gap(now_ms)

    def observe_frontend_turn_finished(self, now_ms: int) -> None:
        if self._presentation_state == "idle":
            return
        self._frontend_turn_finished = True
        if self._presentation_state == "playing" and not self._playback_active:
            # Some integrations report end-of-turn immediately before they
            # append and acknowledge the final TTS chunk. Keep one manager tick
            # of grace so the next backend result cannot enter between them.
            self._presentation_started_at_ms = now_ms
        self._refresh_gap(now_ms)

    def pop_releasable(self, now_ms: int) -> Optional[ReleaseDecision]:
        self._refresh_gap(now_ms)
        if not self._ready or self._queued_work_id is not None:
            return None
        if self._is_blocked(now_ms) or self._gap_started_at_ms is None:
            return None
        oldest_wait_ms = max(now_ms - item.ready_at_ms for item in self._ready)
        required_silence_ms = max(
            self._config.min_silence_ms,
            self._config.initial_silence_ms
            - oldest_wait_ms * self._config.silence_decay,
        )
        if now_ms - self._gap_started_at_ms < required_silence_ms:
            return None
        selected = min(self._ready, key=lambda item: self._scheduling_key(item, now_ms))
        self._ready.remove(selected)
        self._queued_work_id = selected.result.delivery_id
        wait_ms = max(0, now_ms - selected.ready_at_ms)
        return ReleaseDecision(
            result=selected.result,
            ready_wait_ms=wait_ms,
            estimated_speech_ms=selected.estimated_speech_ms,
            laxity_ms=selected.result.expires_at_ms
            - now_ms
            - selected.estimated_speech_ms,
            backlog_size=len(self._ready),
        )

    def mark_consumed(self, work_id: str, now_ms: int) -> None:
        if work_id != self._queued_work_id:
            return
        self._queued_work_id = None
        self._presentation_work_id = work_id
        self._presentation_state = "waiting_frontend_reaction"
        self._presentation_started_at_ms = now_ms
        self._frontend_turn_finished = False
        self._gap_started_at_ms = None

    def discard_queued(self, work_id: str, now_ms: int) -> None:
        if work_id == self._queued_work_id:
            self._queued_work_id = None
            self._refresh_gap(now_ms)

    def discard_feedback(self, feedback_id, now_ms):
        self._ready = [
            item for item in self._ready if item.result.delivery_id != feedback_id
        ]
        self.discard_queued(feedback_id, now_ms)
        if self._presentation_work_id == feedback_id:
            self._clear_presentation()
        self._refresh_gap(now_ms)

    def _scheduling_key(self, item: ReadyItem, now_ms: int):
        wait_ms = max(0, now_ms - item.ready_at_ms)
        if wait_ms >= self._config.max_queue_wait_ms:
            return (0, item.ready_at_ms, item.enqueue_order)
        laxity = item.result.expires_at_ms - now_ms - item.estimated_speech_ms
        ordinary_progress = (
            item.result.feedback_id
            and item.result.status == "pending"
            and item.result.metadata.get("kind")
            not in {"need_input", "approval", "important", "correction"}
        )
        return (
            2 if ordinary_progress else 1,
            laxity,
            item.ready_at_ms,
            item.enqueue_order,
        )

    @property
    def _playback_active(self) -> bool:
        return any(
            playback.played_chars < playback.total_chars
            for playback in self._playback.values()
        )

    def _is_blocked(self, now_ms: int) -> bool:
        return (
            now_ms < self._user_speaking_until_ms
            or self._playback_active
            or self._presentation_state != "idle"
        )

    def _refresh_gap(self, now_ms: int) -> None:
        self._expire_presentation(now_ms)
        if self._is_blocked(now_ms) or self._queued_work_id is not None:
            self._gap_started_at_ms = None
        elif self._gap_started_at_ms is None:
            self._gap_started_at_ms = now_ms

    def _expire_presentation(self, now_ms: int) -> None:
        if self._presentation_state == "waiting_frontend_reaction":
            timeout_ms = self._config.frontend_reaction_timeout_ms
        elif self._presentation_state == "playing":
            timeout_ms = (
                self._config.manager_tick_ms
                if self._frontend_turn_finished and not self._playback_active
                else self._config.max_playback_wait_ms
            )
        else:
            return
        if now_ms - self._presentation_started_at_ms >= timeout_ms:
            self._clear_presentation()

    def _clear_presentation(self) -> None:
        self._presentation_work_id = None
        self._presentation_state = "idle"
        self._presentation_started_at_ms = 0
        self._frontend_turn_finished = False
