"""Bounded-memory MP4 decoding for the browser upload source."""

import io
from dataclasses import dataclass


@dataclass(frozen=True)
class VideoBucket:
    pcm: bytes
    jpeg: bytes


def video_buckets(path):
    """Yield one second of 16 kHz PCM and one JPEG, padding missing audio with silence."""
    import av

    with av.open(str(path)) as video, av.open(str(path)) as audio:
        if not video.streams.video:
            raise ValueError("上传文件没有视频轨道")
        video_stream = video.streams.video[0]
        frames = iter(video.decode(video=0))
        frame = next(frames, None)
        if frame is None:
            raise ValueError("上传视频没有可解码画面")
        start = frame.time or 0.0
        rate = float(video_stream.average_rate or 25)
        frame_index = 0

        def pcm_parts():
            if not audio.streams.audio:
                return
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
            for source in audio.decode(audio=0):
                for converted in resampler.resample(source):
                    yield bytes(converted.planes[0])[: converted.samples * 2]
            for converted in resampler.resample(None):
                yield bytes(converted.planes[0])[: converted.samples * 2]

        parts = iter(pcm_parts())
        pending = bytearray()
        audio_done = False
        second = 0
        last_jpeg = b""
        while True:
            while len(pending) < 32000 and not audio_done:
                part = next(parts, None)
                if part is None:
                    audio_done = True
                else:
                    pending.extend(part)
            selected = None
            while frame is not None:
                timestamp = (
                    frame.time - start if frame.time is not None else frame_index / rate
                )
                if timestamp >= second + 1:
                    break
                selected = frame
                frame = next(frames, None)
                frame_index += 1
            if selected is None and frame is None and not pending:
                break
            if selected is not None:
                image = selected.to_image()
                image.thumbnail((720, 720))
                encoded = io.BytesIO()
                image.save(encoded, format="JPEG", quality=80)
                last_jpeg = encoded.getvalue()
            pcm = bytes(pending[:32000])
            del pending[:32000]
            yield VideoBucket(pcm.ljust(32000, b"\0"), last_jpeg)
            second += 1


def audio_buckets(path):
    """Yield bounded 16 kHz mono PCM buckets, with no visual data."""
    import av
    with av.open(str(path)) as media:
        if not media.streams.audio:
            raise ValueError("上传文件没有音频轨道")
        # Some audio containers (e.g. MP3) carry attached cover art. It is ignored.
        if any(not (stream.disposition & stream.disposition.attached_pic) for stream in media.streams.video):
            raise ValueError("Audio 模式请上传音频文件，而非视频")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        pending = bytearray()
        def parts():
            for source in media.decode(audio=0):
                yield from resampler.resample(source)
            yield from resampler.resample(None)
        for frame in parts():
            pending.extend(bytes(frame.planes[0])[:frame.samples * 2])
            while len(pending) >= 32000:
                yield VideoBucket(bytes(pending[:32000]), b"")
                del pending[:32000]
        if pending:
            yield VideoBucket(bytes(pending).ljust(32000, b"\0"), b"")
