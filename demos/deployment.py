"""Frontend settings owned by the Demo deployment file, not by Harness."""
import json
from pathlib import Path

from demos.launcher.options import load_deployment
from demos.model.settings import DuplexSettings


def frontend_settings(path, fallback=None):
    options = load_deployment(Path(path)) if path else {}
    default = fallback or DuplexSettings()
    return DuplexSettings(options.get("length_penalty", default.length_penalty)), options.get("model_timeout_s", 180)


def updated_frontend_document(path, duplex):
    target = Path(path)
    load_deployment(target)  # Validate without rewriting asset paths as absolute.
    data = json.loads(target.read_text())
    if "model" in data:
        data["model"]["length_penalty"] = duplex.length_penalty
    else:
        data["length_penalty"] = duplex.length_penalty
    return data
