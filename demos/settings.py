"""Demo compatibility for legacy files that included frontend duplex settings."""
import json
from dataclasses import asdict
from pathlib import Path

from demos.model.settings import DuplexSettings
from harness.settings import (
    HarnessSetup, config_path, merge, settings, write_json,
    template as harness_template,
)


def template():
    return {**harness_template(), "duplex": asdict(DuplexSettings())}


class UserSetup(HarnessSetup):
    def __init__(self, data, path):
        if not isinstance(data, dict):
            raise ValueError("Configuration must be an object")
        data = dict(data)
        duplex = data.pop("duplex", {})
        super().__init__(data, path)
        self.duplex = settings(DuplexSettings(), duplex)
        self.data["duplex"] = asdict(self.duplex)


def load_setup(path=None):
    target = config_path(path)
    return UserSetup(json.loads(target.read_text()) if target.exists() else {}, target)


def save_setup(data, path):
    setup = UserSetup(data, config_path(path))
    write_json(setup.path, setup.data)
    return setup
