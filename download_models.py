#!/usr/bin/env python3
"""Download Realtime-Venus checkpoints using the model repository's manifest."""

from __future__ import annotations

import argparse
import math
import re
from pathlib import Path, PurePosixPath

import yaml
from huggingface_hub import HfApi, hf_hub_download, snapshot_download


REPO_ID = "inclusionAI/Realtime-Venus"
MANIFEST = "config.yaml"
MODEL_CHOICES = ("omni", "audio", "all")


def _model_paths(manifest: object) -> dict[str, str]:
    if not isinstance(manifest, dict) or manifest.get("name") != "Realtime-Venus":
        raise ValueError("config.yaml must describe the Realtime-Venus repository.")
    models = manifest.get("models")
    if not isinstance(models, dict):
        raise ValueError("config.yaml must contain a models mapping.")

    paths = {}
    for name in ("omni", "audio"):
        entry = models.get(name)
        value = entry.get("path") if isinstance(entry, dict) else None
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9_-][A-Za-z0-9._/-]*", value
        ):
            raise ValueError(f"Invalid directory for {name} in config.yaml.")
        if any(part in ("", ".", "..") for part in value.split("/")):
            raise ValueError(f"Invalid directory for {name} in config.yaml.")
        paths[name] = value

    omni, audio = (PurePosixPath(paths[name]) for name in ("omni", "audio"))
    if omni == audio or omni in audio.parents or audio in omni.parents:
        raise ValueError("The Omni and Audio directories must not overlap.")
    return paths


def _check_destination(root: Path, filename: str) -> Path:
    """Keep downloaded files and Hub metadata inside the selected directory."""
    relative = PurePosixPath(filename)
    if relative.is_absolute() or any(p in ("", ".", "..") for p in filename.split("/")):
        raise ValueError(f"Invalid repository file path: {filename!r}")
    if "\\" in filename:
        raise ValueError(f"Invalid repository file path: {filename!r}")
    destination = root.joinpath(*relative.parts)
    if not destination.resolve().is_relative_to(root):
        raise ValueError(f"Download path leaves the destination directory: {filename}")
    return destination


def _verify_downloads(
    root: Path,
    filenames: list[str],
    revision: str,
    sizes: dict[str, int | None] | None = None,
) -> None:
    """Reject the Hub's offline fallback to files from an older revision.

    In local-dir mode, huggingface_hub records the commit hash on the first
    line of each .metadata file. Checking it avoids reporting an incomplete
    or stale download as successful after a network failure.
    """
    for filename in filenames:
        destination = _check_destination(root, filename)
        metadata = _check_destination(
            root, f".cache/huggingface/download/{filename}.metadata"
        )
        try:
            with metadata.open(encoding="utf-8") as handle:
                downloaded_revision = handle.readline().strip()
                etag = handle.readline().strip()
                timestamp = float(handle.readline().strip())
            stat = destination.stat()
            expected_size = (sizes or {}).get(filename)
            valid = (
                destination.is_file()
                and downloaded_revision == revision
                and bool(etag)
                and math.isfinite(timestamp)
                and stat.st_mtime <= timestamp + 1
                and (expected_size is None or stat.st_size == expected_size)
            )
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise RuntimeError(
                f"Could not confirm {filename} at revision {revision}. "
                "Check the connection and rerun the same download command; "
                "completed files will be reused."
            )


def download_models(
    model: str = "all",
    local_dir: str | Path = ".",
    revision: str = "main",
    max_workers: int = 8,
    token: str | bool | None = None,
) -> dict[str, Path]:
    """Download omni, audio, or both, returning their local checkpoint paths.

    The root config.yaml is downloaded and read before selecting model files.
    All requests use one resolved commit. Existing up-to-date files are reused
    by huggingface_hub; other local files and model directories are not removed.
    """
    if model not in MODEL_CHOICES:
        raise ValueError(f"model must be one of {', '.join(MODEL_CHOICES)}")
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")

    root = Path(local_dir).expanduser().resolve()
    _check_destination(root, MANIFEST)
    _check_destination(root, ".cache/huggingface")
    _check_destination(root, f".cache/huggingface/download/{MANIFEST}.metadata")
    _check_destination(root, f".cache/huggingface/download/{MANIFEST}.lock")
    info = HfApi().model_info(
        REPO_ID, revision=revision, token=token, files_metadata=True
    )
    commit = info.sha
    if not commit:
        raise RuntimeError(f"Could not resolve revision {revision!r}.")
    sizes = {entry.rfilename: entry.size for entry in info.siblings or []}
    print(f"Repository: {REPO_ID} @ {commit}", flush=True)
    manifest_file = hf_hub_download(
        repo_id=REPO_ID,
        filename=MANIFEST,
        revision=commit,
        local_dir=root,
        token=token,
    )
    _verify_downloads(root, [MANIFEST], commit, sizes)
    with Path(manifest_file).open(encoding="utf-8") as handle:
        paths = _model_paths(yaml.safe_load(handle))
    selected = paths if model == "all" else {model: paths[model]}

    available = set(sizes)
    filenames = sorted(
        filename
        for filename in available
        if any(filename.startswith(f"{path}/") for path in selected.values())
    )
    for name, path in selected.items():
        if f"{path}/config.json" not in available:
            raise ValueError(f"The manifest's {name} directory has no config.json: {path}")
        print(f"{name}: {root / path}", flush=True)
    for filename in filenames:
        _check_destination(root, filename)
        _check_destination(root, f".cache/huggingface/download/{filename}.metadata")
        _check_destination(root, f".cache/huggingface/download/{filename}.lock")

    # The manifest is already present. Fetch only the selected model directories
    # so choosing Audio does not also download the Omni checkpoint.
    snapshot_download(
        repo_id=REPO_ID,
        revision=commit,
        local_dir=root,
        allow_patterns=[f"{path}/*" for path in selected.values()],
        max_workers=max_workers,
        token=token,
    )
    _verify_downloads(root, filenames, commit, sizes)
    return {name: root / path for name, path in selected.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODEL_CHOICES, default="all")
    parser.add_argument("--local-dir", default=".", help="Destination root (default: current directory)")
    parser.add_argument("--revision", default="main", help="Hub branch, tag, or commit (default: main)")
    parser.add_argument("--max-workers", type=int, default=8, help="Concurrent file downloads (default: 8)")
    args = parser.parse_args()
    try:
        paths = download_models(
            model=args.model,
            local_dir=args.local_dir,
            revision=args.revision,
            max_workers=args.max_workers,
        )
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as error:
        parser.exit(1, f"Download failed: {error}\n")
    for name, path in paths.items():
        print(f"Ready: {name} -> {path}")


if __name__ == "__main__":
    main()
