"""Browser input validation and safe playback serialization."""

import base64
import binascii

from harness.bridge.host import PlaybackEnqueue
from harness.bridge.serving import AudioFormat

MAX_AUDIO_BYTES = 128 * 1024
MAX_JPEG_BYTES = 190 * 1024


def decode_media(message: dict, *, kind: str) -> bytes:
    encoded = message.get("data")
    limit = MAX_AUDIO_BYTES if kind == "audio" else MAX_JPEG_BYTES
    if (
        not isinstance(encoded, str)
        or not encoded
        or len(encoded) > (limit + 2) // 3 * 4
    ):
        raise ValueError("媒体数据为空或超过大小限制")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("媒体数据不是有效 Base64") from exc
    if not data or len(data) > limit:
        raise ValueError("媒体数据为空或超过大小限制")
    if kind == "audio" and len(data) % 2:
        raise ValueError("PCM16 音频必须包含完整采样")
    if kind == "video_frame" and not data.startswith(b"\xff\xd8"):
        raise ValueError("摄像头画面必须是 JPEG")
    return data


def playback_message(request: PlaybackEnqueue) -> dict:
    return {
        "type": "playback",
        "utterance_id": request.utterance_id,
        "chunk_seq": request.chunk_seq,
        "text": request.text,
        "data": base64.b64encode(request.audio.data).decode("ascii"),
        "format": request.audio.format.value,
        "sample_rate": request.audio.sample_rate_hz,
        "channels": request.audio.channels,
        # Padding keeps its playback duration and receipt, but should not make
        # the browser report that Venus is speaking.
        "silent": request.audio.format is AudioFormat.PCM_S16LE
        and not any(request.audio.data),
    }
