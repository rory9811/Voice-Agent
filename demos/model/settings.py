"""Lightweight settings shared by the browser and model transport."""

import math
from dataclasses import dataclass


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
