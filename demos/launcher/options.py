"""Deployment JSON and command-line options (separate from web Harness settings)."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

_PATHS = {"model_path", "omni_model_path", "audio_model_path", "reference_audio", "harness_config"}
_INTS = {"model_port", "web_port", "memory_minutes"}
_ALLOWED = _PATHS | _INTS | {"model_type", "web_host", "startup_timeout", "length_penalty", "model_timeout_s"}


def _absolute(value: str, base: Path) -> str:
    return str((base / Path(value).expanduser()).resolve())


def flatten_deployment(options: dict, path: Path) -> dict:
    if not ({"model", "harness", "web"} & options.keys()):
        return options  # Existing flat deployment files remain supported.
    if options.keys() - {"model", "harness", "web", "startup_timeout"}:
        raise ValueError(f"Unknown or mixed deployment sections in {path}")
    sections = {
        "model": {"type": "model_type", "path": "model_path", "reference_audio": "reference_audio",
                  "port": "model_port", "memory_minutes": "memory_minutes",
                  "length_penalty": "length_penalty", "timeout_s": "model_timeout_s"},
        "harness": {"config": "harness_config"},
        "web": {"host": "web_host", "port": "web_port"},
    }
    result = {"startup_timeout": options["startup_timeout"]} if "startup_timeout" in options else {}
    for section, keys in sections.items():
        data = options.get(section, {})
        if not isinstance(data, dict) or data.keys() - keys.keys():
            raise ValueError(f"Unknown or invalid {section} settings in {path}")
        result.update({keys[k]: v for k, v in data.items()})
    if not result.get("harness_config"):
        raise ValueError(f"Set harness.config to a Harness JSON file in {path}")
    return result


def load_deployment(path: Path, *, required: bool = True) -> dict:
    if not path.exists() and not required:
        return {}
    try:
        options = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read deployment config {path}: {exc}") from exc
    if not isinstance(options, dict):
        raise ValueError(f"Deployment config {path} must contain a JSON object")
    options = flatten_deployment(options, path)
    unknown = options.keys() - _ALLOWED
    if unknown:
        raise ValueError(f"Unknown deployment settings in {path}: {', '.join(sorted(unknown))}")
    for key, value in options.items():
        if key in _INTS:
            valid = type(value) is int and value > 0
            if key.endswith("_port"):
                valid = valid and value <= 65535
        elif key in {"startup_timeout", "length_penalty", "model_timeout_s"}:
            valid = type(value) in (int, float) and math.isfinite(value) and value > 0
            if key == "length_penalty":
                valid = valid and 0.1 <= value <= 5.0
        else:
            valid = isinstance(value, str)
            if key == "model_type":
                valid = value in ("audio", "video", "omni")
            elif key == "web_host":
                valid = valid and bool(value.strip())
        if not valid:
            raise ValueError(f"Invalid {key} in deployment config {path}")
    for key in _PATHS:
        if options.get(key):
            options[key] = _absolute(options[key], path.parent)
    return options


def parse_launch_options(root: Path, argv=None):
    launch_argv = list(sys.argv[1:] if argv is None else argv)
    caller = Path(os.getenv("VENUS_LAUNCH_CWD", str(Path.cwd()))).resolve()
    parser = argparse.ArgumentParser(
        description="Launch the Realtime-Venus model and browser demo.", allow_abbrev=False
    )
    parser.add_argument("model_type", nargs="?", choices=("audio", "omni", "video"),
                        help="Override model_type; video is an alias for omni")
    parser.add_argument("--config", help="Deployment JSON (default: project-root config.json)")
    parser.add_argument("--model-path")
    parser.add_argument("--harness-config", help="Override the referenced Harness JSON file")
    parser.add_argument("--ref-audio")
    parser.add_argument("--model-port", type=int)
    parser.add_argument("--web-port", type=int)
    parser.add_argument("--host")
    parser.add_argument("--memory-minutes", type=int)
    parser.add_argument("--startup-timeout", type=float)
    parser.add_argument("--no-login", action="store_true",
                        help="Fail immediately if Codex is not authenticated")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--detach", action="store_true", help="Leave both services running after ready")
    actions.add_argument("--stop", action="store_true", help="Stop the stack owned by this checkout")
    actions.add_argument("--status", action="store_true", help="Show this checkout's stack status")
    actions.add_argument("--check", action="store_true", help="Check assets, ports, CUDA and backend without starting services")
    args = parser.parse_args(launch_argv)
    # Status and shutdown must remain usable even if the deployment JSON was removed.
    if args.status or args.stop:
        return parser, args, {}, launch_argv
    path = Path(_absolute(args.config, caller)) if args.config else root / "config.json"
    options = load_deployment(path, required=bool(args.config))
    mode = args.model_type or os.getenv("VENUS_MODEL_TYPE") or options.get("model_type", "omni")
    if mode not in ("audio", "video", "omni"):
        parser.error("model_type must be audio, video or omni")
    args.model_type = "omni" if mode == "video" else mode

    def resolve(cli_value, env_names, config_names, default=None, *, is_path=False):
        if cli_value is not None:
            value = cli_value
        else:
            value = next((os.environ[k] for k in env_names if os.environ.get(k)), None)
            if value is None:
                # Config paths were already resolved relative to the JSON file.
                return next((options[k] for k in config_names if options.get(k) not in (None, "")), default)
        return _absolute(value, caller) if is_path and value else value

    args.model_path = resolve(args.model_path,
        (f"{args.model_type.upper()}_MODEL_PATH", "MODEL_PATH"),
        (f"{args.model_type}_model_path", "model_path"), is_path=True)
    args.harness_config = resolve(args.harness_config, ("HARNESS_CONFIG",), ("harness_config",), is_path=True)
    if args.harness_config and not Path(args.harness_config).is_file():
        parser.error(f"Harness config does not exist: {args.harness_config}; set harness.config to an existing Harness configuration file; see the Harness README")
    args.demo_config = path if path.exists() else None
    args.ref_audio = resolve(args.ref_audio, ("REF_AUDIO",), ("reference_audio",), is_path=True)
    for attr, env, key, default, cast in (
        ("model_port", "VENUS_MODEL_PORT", "model_port", 8031, int),
        ("web_port", "VENUS_WEB_PORT", "web_port", 8032, int),
        ("host", "VENUS_WEB_HOST", "web_host", "127.0.0.1", str),
        ("memory_minutes", "VENUS_MEMORY_MINUTES", "memory_minutes", 40, int),
        ("startup_timeout", "VENUS_STARTUP_TIMEOUT", "startup_timeout", 600, float),
    ):
        try:
            value = cast(resolve(getattr(args, attr), (env,), (key,), default))
        except (ValueError, TypeError):
            parser.error(f"Invalid {key}")
        if attr in ("model_port", "web_port") and not 1 <= value <= 65535:
            parser.error(f"{key} must be between 1 and 65535")
        if attr in ("memory_minutes", "startup_timeout") and (not math.isfinite(value) or value <= 0):
            parser.error(f"{key} must be positive and finite")
        if attr == "host" and not value.strip():
            parser.error("web_host must not be empty")
        setattr(args, attr, value)
    if args.model_port == args.web_port:
        parser.error("model_port and web_port must be different")

    # The background child runs from the project root. Freeze resolved arguments
    # so a caller-relative config/asset path cannot change meaning after detach.
    child_argv = [args.model_type, "--model-port", str(args.model_port),
                  "--web-port", str(args.web_port), "--host", args.host,
                  "--memory-minutes", str(args.memory_minutes),
                  "--startup-timeout", str(args.startup_timeout)]
    if args.config or path.exists():
        child_argv += ["--config", str(path)]
    if args.harness_config:
        child_argv += ["--harness-config", args.harness_config]
    if args.model_path:
        child_argv += ["--model-path", args.model_path]
    if args.ref_audio:
        child_argv += ["--ref-audio", args.ref_audio]
    return parser, args, options, child_argv
