"""Compose the model connection and asynchronous Harness for a browser session."""

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from demos.model.client import RemoteOmniServingPort
from demos.model.settings import TOKEN_ENV, model_token
from demos.settings import load_setup
from demos.variants import model_name
from harness.bridge.runtime import VenusOmniAgentHarness
from harness.factory import build_harness


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
    return (
        f"Cannot reach a ready model server at {server_url}. Check this computer's VPN or "
        "network connection to the server, and start the model there with `docker compose up -d` in deploy/model."
    )


def _model_client(server_url, token, timeout_s):
    # The model URL is a direct endpoint on the VPN or local network, never a proxy target.
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=server_url, timeout=timeout_s, trust_env=False, headers=headers)


def check_model(server_url, token=None, timeout_s=30.0):
    """Return the model server's health once it is reachable, ready and accepts the token."""
    try:
        with _model_client(server_url, token, timeout_s) as client:
            response = client.get("/healthz")
            health = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError(unreachable_message(server_url)) from exc
    if response.status_code != 200 or health.get("ready") is not True:
        raise RuntimeError(unreachable_message(server_url))
    if "token_ok" not in health:
        raise RuntimeError(
            f"The model server at {server_url} runs an older version of this code; "
            "update the code on the server and restart the model."
        )
    if health["token_ok"] is not True:
        raise RuntimeError(
            f"The model server at {server_url} rejected the token. Set {TOKEN_ENV} to the "
            "same secret on the model server and this computer, then restart both."
        )
    return health


def fetch_model(server_url, token=None, timeout_s=30.0):
    """Read the served model type and load its tokenizer from the model server."""
    from transformers import Qwen2TokenizerFast

    health = check_model(server_url, token, timeout_s)
    try:
        with _model_client(server_url, token, timeout_s) as client:
            files = client.get("/tokenizer").raise_for_status().json()["files"]
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise RuntimeError(f"Cannot download the tokenizer from {server_url}: {exc}") from exc
    with tempfile.TemporaryDirectory(prefix="venus-tokenizer-") as directory:
        for name, text in files.items():
            (Path(directory) / Path(name).name).write_text(text, encoding="utf-8")
        tokenizer = Qwen2TokenizerFast.from_pretrained(directory)
    return ModelInfo(health["model_type"], tokenizer)


async def real_resources(*, mode, settings_path, server_url, tokenizer, model_type, model_timeout_s):
    if mode not in {"omni", "audio"}:
        raise ValueError("Unknown input mode")
    setup = load_setup(settings_path)
    agent = build_harness(setup)
    return WebResources(
        agent,
        RemoteOmniServingPort(
            server_url,
            timeout_s=model_timeout_s,
            length_penalty=setup.duplex.length_penalty,
            token=model_token(),
        ),
        tokenizer,
        model=model_name(model_type),
    )
