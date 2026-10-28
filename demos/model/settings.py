"""Lightweight settings shared by the browser and model transport."""

import math
import os
from dataclasses import dataclass

# Shared secret for the model API. Both machines read it from the environment,
# so it never lands in a tracked config file or in a process's argv.
TOKEN_ENV = "VENUS_MODEL_TOKEN"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def model_token():
    return os.environ.get(TOKEN_ENV, "").strip() or None


def require_token(host, token):
    """The model API has no other access control, so never serve it unguarded."""
    if host not in _LOOPBACK and not token:
        raise ValueError(
            f"Set {TOKEN_ENV} to the same secret on the model server and this computer "
            f"before serving the model on {host}"
        )


@dataclass(frozen=True)
class DuplexSettings:
    length_penalty: float = 0.8

    def __post_init__(self):
        value = self.length_penalty
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0.1 <= value <= 5.0
        ):
            raise ValueError("length_penalty must be a finite number from 0.1 to 5.0")
