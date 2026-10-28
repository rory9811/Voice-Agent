"""Prepare buffered PCM/WAV and video evidence for provider adapters."""

from __future__ import annotations

import io
import wave
from typing import Optional, Sequence

from .errors import MediaError
from .models import (
    AudioChunk,
    BinaryAsset,
    ContextSnapshot,
    InputMode,
    PreparedContext,
    VideoFrame,
    VideoInput,
    VideoSegment,
)


def assemble_audio(
    chunks: Sequence[AudioChunk],
    *,
    window_start_ms: Optional[int] = None,
    cutoff_ms: Optional[int] = None,
) -> Optional[BinaryAsset]:
    """Join homogeneous PCM16LE or WAV chunks into one WAV file.

    The harness does not run an audio encoder. Integrations must provide a
    stable PCM16LE/WAV ingress format. This conversion only adds/removes WAV
    container headers so providers receive a normal ``audio/wav`` payload.
    """

    if not chunks:
        return None
    frames = []
    params = None
    for chunk in chunks:
        if chunk.mime_type in ("audio/pcm", "audio/L16"):
            current = (
                chunk.channels,
                chunk.sample_width_bytes,
                chunk.sample_rate_hz,
            )
            payload = chunk.data
        elif chunk.mime_type in ("audio/wav", "audio/x-wav"):
            try:
                with wave.open(io.BytesIO(chunk.data), "rb") as reader:
                    current = (
                        reader.getnchannels(),
                        reader.getsampwidth(),
                        reader.getframerate(),
                    )
                    payload = reader.readframes(reader.getnframes())
            except (wave.Error, EOFError) as exc:
                raise MediaError("invalid WAV audio chunk") from exc
        else:
            raise MediaError("V1 audio ingress only supports PCM16LE or WAV chunks")
        if params is None:
            params = current
        elif params != current:
            raise MediaError("audio chunks in one context window use different formats")
        if window_start_ms is not None and cutoff_ms is not None:
            payload = _clip_pcm_to_window(
                payload,
                chunk=chunk,
                channels=current[0],
                sample_width=current[1],
                sample_rate=current[2],
                window_start_ms=window_start_ms,
                cutoff_ms=cutoff_ms,
            )
        if payload:
            frames.append(payload)

    if params is None:
        return None
    channels, sample_width, sample_rate = params
    if sample_width != 2:
        raise MediaError("V1 audio ingress requires 16-bit PCM samples")
    if not frames:
        return None
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(b"".join(frames))
    return BinaryAsset(data=output.getvalue(), mime_type="audio/wav")


def _clip_pcm_to_window(
    payload: bytes,
    *,
    chunk: AudioChunk,
    channels: int,
    sample_width: int,
    sample_rate: int,
    window_start_ms: int,
    cutoff_ms: int,
) -> bytes:
    """Trim a straddling PCM chunk so the upload cannot include future audio."""

    overlap_start = max(window_start_ms, chunk.start_ms)
    overlap_end = min(cutoff_ms, chunk.end_ms)
    if overlap_end <= overlap_start:
        return b""
    frame_bytes = channels * sample_width
    start_frame = round((overlap_start - chunk.start_ms) * sample_rate / 1_000)
    end_frame = round((overlap_end - chunk.start_ms) * sample_rate / 1_000)
    start_offset = max(0, start_frame * frame_bytes)
    end_offset = min(len(payload), max(start_offset, end_frame * frame_bytes))
    return payload[start_offset:end_offset]


def select_video(
    segments: Sequence[VideoSegment],
    frames: Sequence[VideoFrame],
    *,
    window_start_ms: int,
    cutoff_ms: int,
    max_frames: int,
) -> Optional[VideoInput]:
    """Prefer a direct segment covering the window; otherwise evenly sample frames."""

    covering = [
        segment
        for segment in segments
        if segment.start_ms <= window_start_ms and segment.end_ms >= cutoff_ms
    ]
    if covering:
        return VideoInput(segment=max(covering, key=lambda item: item.end_ms))
    if segments:
        # A deployment may provide rolling segments shorter than the policy
        # window. Passing the latest segment is more truthful than remuxing
        # unsupported containers in the harness.
        return VideoInput(segment=max(segments, key=lambda item: item.end_ms))
    if not frames:
        return None
    selected = _evenly_sample(frames, max_frames)
    return VideoInput(frames=tuple(selected))


def _evenly_sample(
    frames: Sequence[VideoFrame], max_frames: int
) -> Sequence[VideoFrame]:
    if len(frames) <= max_frames:
        return frames
    if max_frames <= 1:
        return [frames[-1]]
    indexes = [
        round(index * (len(frames) - 1) / (max_frames - 1))
        for index in range(max_frames)
    ]
    return [frames[index] for index in indexes]


def prepare_context(
    snapshot: ContextSnapshot,
    max_video_frames: int,
    input_mode: InputMode = InputMode.TEXT_MULTIMODAL,
) -> PreparedContext:
    """Build only the media explicitly selected by the routing decision."""

    audio = None
    video = None
    if input_mode in (InputMode.TEXT_AUDIO, InputMode.TEXT_MULTIMODAL):
        audio = assemble_audio(
            snapshot.audio_chunks,
            window_start_ms=snapshot.window_start_ms,
            cutoff_ms=snapshot.cutoff_ms,
        )
    if input_mode is InputMode.TEXT_MULTIMODAL:
        video = select_video(
            snapshot.video_segments,
            snapshot.video_frames,
            window_start_ms=snapshot.window_start_ms,
            cutoff_ms=snapshot.cutoff_ms,
            max_frames=max_video_frames,
        )
    return PreparedContext(
        snapshot=snapshot,
        audio=audio,
        video=video,
        played_tts_text=snapshot.played_tts_text,
    )
