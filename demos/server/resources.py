"""Compose the model connection and asynchronous Harness for a browser session."""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from demos.model.client import RemoteOmniServingPort
from demos.settings import load_setup
from demos.variants import model_name
from harness.bridge.runtime import VenusOmniAgentHarness
from harness.factory import build_harness
from demos.deployment import frontend_settings


@dataclass
class WebResources:
    agent: VenusOmniAgentHarness
    serving: RemoteOmniServingPort
    tokenizer: Any
    model: str = "Realtime-Venus-Omni"

    async def close(self):
        try:
            await self.agent.aclose()
        finally:
            await self.serving.aclose()


@dataclass(frozen=True)
class ModelInfo:
    model_type: str
    tokenizer: Any


def unreachable_message(server_url):
    port = urlsplit(server_url).port or 8031
    return (
        f"Cannot reach a ready model server at {server_url}. Start it on the GPU server "
        f"with `bash start.sh model`, then keep `ssh -N -L {port}:127.0.0.1:{port} user@server` "
        "running on this computer."
    )


def fetch_model(server_url, timeout_s=30.0):
    """Read the served model type and load its tokenizer from the model server."""
    from transformers import Qwen2TokenizerFast

    # The model URL is a direct endpoint (usually an SSH tunnel), never a proxy target.
    with httpx.Client(base_url=server_url, timeout=timeout_s, trust_env=False) as client:
        try:
            health = client.get("/healthz")
            if health.status_code != 200 or health.json().get("ready") is not True:
                raise RuntimeError(unreachable_message(server_url))
            files = client.get("/tokenizer").raise_for_status().json()["files"]
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise RuntimeError(unreachable_message(server_url)) from exc
    with tempfile.TemporaryDirectory(prefix="venus-tokenizer-") as directory:
        for name, text in files.items():
            (Path(directory) / Path(name).name).write_text(text, encoding="utf-8")
        tokenizer = Qwen2TokenizerFast.from_pretrained(directory)
    return ModelInfo(health.json()["model_type"], tokenizer)


async def real_resources(*, mode, settings_path, server_url, tokenizer, model_type, demo_path=None):
    if mode not in {"omni", "audio"}:
        raise ValueError("Unknown input mode")
    setup = load_setup(settings_path)
    duplex, model_timeout = frontend_settings(demo_path, setup.duplex)
    agent = build_harness(setup)
    return WebResources(
        agent,
        RemoteOmniServingPort(
            server_url, timeout_s=model_timeout, length_penalty=duplex.length_penalty
        ),
        tokenizer,
        model=model_name(model_type),
    )
