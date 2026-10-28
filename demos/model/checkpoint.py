"""Validate a downloaded checkpoint before the model server loads it."""

from __future__ import annotations

import json
from pathlib import Path

from demos.variants import model_name


def checked_file(path: Path) -> Path:
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Missing or empty model asset: {path}")
    with path.open("rb") as stream:
        if stream.read(128).startswith(b"version https://git-lfs.github.com/spec/"):
            raise ValueError(f"Model asset is still a Git LFS pointer: {path}")
    return path


def checkpoint_directory(path: Path, model_type: str | None = None) -> Path:
    path = path.expanduser().resolve()
    if model_type and not (path / "config.json").is_file():
        for candidate in (path / model_name(model_type), path / "model_weight" / model_name(model_type)):
            if (candidate / "config.json").is_file():
                path = candidate
                break
    if (
        not (path / "config.json").is_file()
        and (path / "model_weight/config.json").is_file()
    ):
        path /= "model_weight"
    checked_file(path / "config.json")
    config = json.loads((path / "config.json").read_text())
    if not isinstance(config, dict):
        raise ValueError("Model config.json must contain an object")
    architecture = config.get("model_type")
    checkpoint_type = {"realtime_venus_omni": "omni", "minicpmo": "audio"}.get(architecture)
    if model_type and checkpoint_type and model_type != checkpoint_type:
        raise ValueError(f"Selected {model_type}, but {path} contains {checkpoint_type} weights; use --model-path with {model_name(model_type)}")
    checked_file(path / "tokenizer.json")
    index = path / "model.safetensors.index.json"
    if index.exists():
        files = set(json.loads(index.read_text()).get("weight_map", {}).values())
    else:
        files = {p.name for p in path.glob("*.safetensors")}
    if not files:
        raise ValueError(f"No merged model weights found in {path}")
    for name in files:
        target = (path / name).resolve()
        if not target.is_relative_to(path):
            raise ValueError("Weight index refers outside the model directory")
        checked_file(target)
    for name in [
        "flow.yaml",
        "flow.pt",
        "hift.pt",
        "campplus.onnx",
        "speech_tokenizer_v2_25hz.onnx",
    ]:
        checked_file(path / "assets/token2wav" / name)
    return path
