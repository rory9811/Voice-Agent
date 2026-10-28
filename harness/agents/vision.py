"""Bounded visual evidence from the immutable delegate snapshot, never live buffers."""

from __future__ import annotations

import io
import math
import time
from dataclasses import dataclass

from .contracts import AgentImage, AgentInput


def constrain_visual_result(result, evidence, language):
    from dataclasses import replace

    if evidence.get("status") not in {"missing", "partial"}:
        return result
    text = (
        "视觉依据缺失或不完整，依赖画面的结论尚未核实。"
        if language != "en"
        else "Visual evidence is missing or incomplete; conclusions requiring images remain unverified."
    )
    return replace(
        result,
        outcome="partial" if result.outcome == "completed" else result.outcome,
        unresolved=result.unresolved
        if text in result.unresolved
        else (*result.unresolved, text),
    )


@dataclass(frozen=True)
class VisionConfig:
    fps: float = 1
    window_s: float = 5
    max_frames: int = 5
    max_images: int = 12
    max_dimension: int = 1280
    max_image_bytes: int = 2_000_000
    max_source_bytes: int = 64_000_000
    decode_timeout_s: float = 10
    max_decode_frames: int = 12000

    def __post_init__(self):
        for key in ("fps", "window_s", "decode_timeout_s"):
            value = getattr(self, key)
            if (
                not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{key} must be finite and positive")
        for key in (
            "max_frames",
            "max_images",
            "max_dimension",
            "max_image_bytes",
            "max_source_bytes",
            "max_decode_frames",
        ):
            if type(getattr(self, key)) is not int or getattr(self, key) <= 0:
                raise ValueError(f"{key} must be a positive integer")
        if self.max_images < self.max_frames:
            raise ValueError("max_images must include the frame budget")


def _encode(image, config):
    from PIL import Image, ImageOps

    image = ImageOps.exif_transpose(image)
    if image.width * image.height > 40_000_000:
        raise ValueError("image dimensions exceed decoding budget")
    image.thumbnail((config.max_dimension, config.max_dimension))
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        canvas = Image.new("RGB", image.size, "white")
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        image = canvas
    else:
        image = image.convert("RGB")
    for _ in range(8):
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=85)
        if len(output.getvalue()) <= config.max_image_bytes:
            return output.getvalue()
        if min(image.size) <= 32:
            break
        image.thumbnail((max(1, image.width // 2), max(1, image.height // 2)))
    raise ValueError("image exceeds byte budget")


def _image_bytes(data, config):
    from PIL import Image

    if len(data) > config.max_source_bytes:
        raise ValueError("image input exceeds source budget")
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 40_000_000:
            raise ValueError("image dimensions exceed decoding budget")
        return _encode(image, config)


def prepare_visual(snapshot, mode, config):
    """Return sanitized files, native image descriptors and factual availability metadata."""
    if mode not in {"text", "text_multimodal"}:
        raise ValueError("invalid General input_mode")
    inputs = [
        AgentInput(f.name, f.data, f.mime_type)
        for f in snapshot.input_files
        if not f.mime_type.startswith(("image/", "audio/", "video/"))
    ]
    evidence = {
        "input_mode": mode,
        "window_start_ms": max(
            snapshot.window_start_ms, snapshot.cutoff_ms - config.window_s * 1000
        ),
        "cutoff_ms": snapshot.cutoff_ms,
        "fps": config.fps,
        "issues": [],
        "images": [],
        "status": "not_requested",
    }
    images = []
    if mode == "text":
        return tuple(inputs), (), evidence
    used = {f.name for f in inputs}

    def add(data, timestamp, source, original_name=None):
        name = f"general-visual-{len(images):03d}.jpg"
        while name in used:
            name = "_" + name
        used.add(name)
        inputs.append(AgentInput(name, data, "image/jpeg"))
        images.append(AgentImage(name, timestamp, source))
        evidence["images"].append(
            {
                "name": name,
                "captured_at_ms": timestamp,
                "source": source,
                "original_name": original_name,
            }
        )

    # An uploaded still is already a complete observation; it is not time-sampled.
    for item in snapshot.input_files:
        if not item.mime_type.startswith("image/"):
            continue
        if len(images) >= config.max_images:
            evidence["issues"].append("image_limit")
            break
        try:
            add(_image_bytes(item.data, config), None, "uploaded_image", item.name)
        except Exception:  # noqa: BLE001 - malformed media is missing evidence, not a worker crash
            evidence["issues"].append("invalid_uploaded_image")
    start, cutoff = evidence["window_start_ms"], snapshot.cutoff_ms
    period = 1000 / config.fps
    bins = {}  # Latest observation in each half-open interval ending at T0.

    def candidate(timestamp, encode):
        if not start < timestamp <= cutoff:
            return
        bucket = int((cutoff - timestamp) // period)
        if bucket >= config.max_frames or (
            bucket in bins and bins[bucket][0] >= timestamp
        ):
            return
        try:
            bins[bucket] = (timestamp, encode())
        except Exception:  # noqa: BLE001 - preserve usable frames and an explicit gap
            evidence["issues"].append("invalid_frame")

    for frame in sorted(
        snapshot.video_frames, key=lambda f: f.captured_at_ms, reverse=True
    ):
        candidate(
            frame.captured_at_ms, lambda frame=frame: _image_bytes(frame.data, config)
        )
    # Prefer supplied timestamped frames. Decode segments only when frame ingress is absent.
    if not bins and snapshot.video_segments:
        deadline = time.monotonic() + config.decode_timeout_s
        decoded_count = 0
        for segment in sorted(
            snapshot.video_segments, key=lambda v: v.end_ms, reverse=True
        ):
            if segment.end_ms <= start or segment.start_ms > cutoff:
                continue
            try:
                if len(segment.data) > config.max_source_bytes:
                    raise ValueError("segment exceeds source budget")
                import av

                fmt = {"video/mp4": "mp4", "video/webm": "matroska"}.get(
                    segment.mime_type
                )
                if fmt is None:
                    raise ValueError("unsupported video container")
                with av.open(io.BytesIO(segment.data), format=fmt) as container:
                    stream = container.streams.video[0]
                    origin = float((stream.start_time or 0) * stream.time_base)
                    for frame in container.decode(stream):
                        decoded_count += 1
                        if (
                            time.monotonic() > deadline
                            or decoded_count > config.max_decode_frames
                        ):
                            raise TimeoutError("video decode budget exceeded")
                        if frame.pts is None or frame.time_base is None:
                            raise ValueError("video lacks timestamps")
                        timestamp = round(
                            segment.start_ms
                            + (float(frame.pts * frame.time_base) - origin) * 1000,
                            3,
                        )
                        if timestamp > min(cutoff, segment.end_ms):
                            break
                        if frame.width * frame.height > 40_000_000:
                            raise ValueError("video dimensions exceed budget")
                        candidate(
                            timestamp,
                            lambda frame=frame: _encode(frame.to_image(), config),
                        )
            except Exception:  # noqa: BLE001 - never expose raw decoder errors to the model
                evidence["issues"].append("video_decode_failed")
            if time.monotonic() > deadline or decoded_count > config.max_decode_frames:
                break
    for timestamp, data in sorted(bins.values()):
        if len(images) >= config.max_images:
            evidence["issues"].append("image_limit")
            break
        add(data, timestamp, "video_frame")
    evidence["issues"] = sorted(set(evidence["issues"]))
    evidence["status"] = (
        "missing" if not images else "partial" if evidence["issues"] else "available"
    )
    return tuple(inputs), tuple(images), evidence
