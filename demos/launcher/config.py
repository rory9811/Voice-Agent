"""Validate checkpoint assets and listening ports for the launcher."""

from __future__ import annotations

import json
import socket
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


def local_url(host: str, port: int, path: str) -> str:
    """URL this machine uses to reach its own service listening on host:port."""
    return f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}{path}"


def ensure_port_available(host: str, port: int) -> None:
    if not 0 < port < 65536:
        raise ValueError(f"Invalid port: {port}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind((host, port))
        except OSError as exc:
            raise ValueError(
                f"Port {host}:{port} is occupied or unavailable; choose another port"
            ) from exc
