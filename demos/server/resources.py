"""Compose the model connection and asynchronous Harness for a browser session."""

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

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


@lru_cache(maxsize=2)
def load_tokenizer(path):
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(
        path, trust_remote_code=True, local_files_only=True
    )


async def real_resources(*, mode, settings_path, server_url, tokenizer_path, model_type="omni", demo_path=None):
    if mode not in {"omni", "audio"}:
        raise ValueError("Unknown input mode")
    tokenizer = await asyncio.to_thread(
        load_tokenizer, str(Path(tokenizer_path).resolve())
    )
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
