"""Deterministic context-selection policy for a delegate trigger."""

from __future__ import annotations

from uuid import uuid4

from .buffers import SessionBuffers
from .config import BufferConfig
from .models import ContextSnapshot


class ContextWindowPolicy:
    """Freeze only pre-trigger user evidence for one delegate request.

    The opening ``<delegate>`` tag defines the temporal boundary.  The closing
    tag only supplies the query; therefore provider latency and later media
    ingress can never expand the selected window.
    """

    def __init__(self, config: BufferConfig) -> None:
        self._config = config

    def capture(
        self,
        *,
        session_id: str,
        buffers: SessionBuffers,
        cutoff_ms: int,
        cutoff_sequence: int,
    ) -> ContextSnapshot:
        window_start_ms = cutoff_ms - self._config.context_window_ms
        audio_chunks = tuple(
            chunk
            for chunk in buffers.audio.values_before(cutoff_sequence)
            if chunk.end_ms > window_start_ms and chunk.start_ms < cutoff_ms
        )
        # Encoded segments cannot safely be sliced in Python without knowing
        # their container/keyframe layout. Require a segment to end by T0 so it
        # cannot contain future video. Frame fallback is exact by timestamp.
        video_segments = tuple(
            segment
            for segment in buffers.video_segments.values_before(cutoff_sequence)
            if segment.end_ms > window_start_ms
            and segment.start_ms < cutoff_ms
            and segment.end_ms <= cutoff_ms
        )
        video_frames = tuple(
            frame
            for frame in buffers.video_frames.values_before(cutoff_sequence)
            if window_start_ms <= frame.captured_at_ms <= cutoff_ms
        )
        return ContextSnapshot(
            session_id=session_id,
            snapshot_id=str(uuid4()),
            cutoff_ms=cutoff_ms,
            cutoff_sequence=cutoff_sequence,
            audio_chunks=audio_chunks,
            video_segments=video_segments,
            video_frames=video_frames,
            played_tts_text=buffers.played_tts.recent_text(
                cutoff_ms,
                self._config.played_tts_max_chars,
            ),
            window_start_ms=window_start_ms,
        )
