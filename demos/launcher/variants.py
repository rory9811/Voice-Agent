from pathlib import Path
from demos.variants import model_name
from .config import checkpoint_directory

def select_checkpoint(root, model_type, explicit=None, configured=None):
    name = model_name(model_type)
    if explicit:
        return checkpoint_directory(root / Path(explicit).expanduser(), model_type)
    candidates = []
    if configured:
        base = root / Path(configured).expanduser()
        # Existing deployments may name the Omni subdirectory. Its Audio sibling
        # is a distinct checkpoint, never a flag on the Omni weights.
        if base.name in {"Realtime-Venus-Omni", "Realtime-Venus-Audio"}:
            candidates.append(base.parent / name)
        candidates.append(base)
    candidates += [root / name, root / "model_weight" / name, root / "model_weight"]
    errors = []
    for candidate in dict.fromkeys(candidates):
        if not candidate.exists():
            continue
        try:
            return checkpoint_directory(candidate, model_type)
        except ValueError as exc:
            errors.append(str(exc))
    detail = errors[-1] if errors else "No checkpoint directory found"
    raise ValueError(f"{detail}. Supply --model-path /path/to/{name} (or the downloaded HF repository root).")
