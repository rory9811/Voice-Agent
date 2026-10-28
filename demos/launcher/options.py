"""Deployment JSON and command-line options for the model and web roles."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_CONFIG = {"model": "model_server.json", "web": "config.json"}
DEFAULT_MODEL_URL = "http://127.0.0.1:8031"
_MODEL_KEYS = {"type", "path", "reference_audio", "host", "port", "memory_minutes", "startup_timeout"}
_WEB_SECTIONS = {
    "model": {"url": "model_url", "length_penalty": "length_penalty", "timeout_s": "model_timeout_s"},
    "harness": {"config": "harness_config"},
    "web": {"host": "web_host", "port": "web_port"},
}


def _absolute(value: str, base: Path) -> str:
    return str((base / Path(value).expanduser()).resolve())


def _positive(value, *, integer=False, upper=None) -> bool:
    if integer:
        valid = type(value) is int and value > 0
    else:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    return valid and (upper is None or value <= upper)


def _port(value) -> bool:
    return _positive(value, integer=True, upper=65535)


def http_url(value) -> bool:
    if not isinstance(value, str):
        return False
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.hostname)


def _read(path: Path) -> dict:
    try:
        options = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read deployment config {path}: {exc}") from exc
    if not isinstance(options, dict):
        raise ValueError(f"Deployment config {path} must contain a JSON object")
    return options


def load_model_options(path: Path) -> dict:
    """Validate model_server.json; paths become absolute, relative to the file."""
    options = _read(path)
    unknown = options.keys() - _MODEL_KEYS
    if unknown:
        raise ValueError(f"Unknown model server settings in {path}: {', '.join(sorted(unknown))}")
    checks = {
        "type": lambda v: v in ("audio", "video", "omni"),
        "path": lambda v: isinstance(v, str),
        "reference_audio": lambda v: isinstance(v, str),
        "host": lambda v: isinstance(v, str) and bool(v.strip()),
        "port": _port,
        "memory_minutes": lambda v: _positive(v, integer=True),
        "startup_timeout": _positive,
    }
    for key, value in options.items():
        if not checks[key](value):
            raise ValueError(f"Invalid {key} in model server config {path}")
    for key in ("path", "reference_audio"):
        if options.get(key):
            options[key] = _absolute(options[key], path.parent)
    return options


def load_web_options(path: Path) -> dict:
    """Validate the local config.json and flatten its sections."""
    options = _read(path)
    if options.keys() - {*_WEB_SECTIONS, "startup_timeout"}:
        raise ValueError(f"Unknown deployment sections in {path}; see demos/README.md for the format")
    result = {"startup_timeout": options["startup_timeout"]} if "startup_timeout" in options else {}
    for section, keys in _WEB_SECTIONS.items():
        data = options.get(section, {})
        if not isinstance(data, dict) or data.keys() - keys.keys():
            raise ValueError(f"Unknown or invalid {section} settings in {path}")
        result.update({keys[k]: v for k, v in data.items()})
    if not result.get("harness_config"):
        raise ValueError(f"Set harness.config to a Harness JSON file in {path}")
    checks = {
        "model_url": http_url,
        "length_penalty": lambda v: _positive(v) and 0.1 <= v <= 5.0,
        "model_timeout_s": _positive,
        "harness_config": lambda v: isinstance(v, str),
        "web_host": lambda v: isinstance(v, str) and bool(v.strip()),
        "web_port": _port,
        "startup_timeout": _positive,
    }
    for key, value in result.items():
        if not checks[key](value):
            raise ValueError(f"Invalid {key} in deployment config {path}")
    result["harness_config"] = _absolute(result["harness_config"], path.parent)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch one Realtime-Venus role.", allow_abbrev=False
    )
    roles = parser.add_subparsers(dest="role", required=True)
    model = roles.add_parser("model", allow_abbrev=False,
                             help="GPU server: load the model and serve its API on 127.0.0.1")
    web = roles.add_parser("web", allow_abbrev=False,
                           help="Local computer: browser service, Harness and Codex")
    for role, sub in (("model", model), ("web", web)):
        sub.add_argument("--config", help=f"Deployment JSON (default: project-root {DEFAULT_CONFIG[role]})")
        sub.add_argument("--port", type=int)
        sub.add_argument("--startup-timeout", type=float)
        actions = sub.add_mutually_exclusive_group()
        actions.add_argument("--detach", action="store_true", help="Leave the service running after ready")
        actions.add_argument("--stop", action="store_true", help="Stop this role's service in this checkout")
        actions.add_argument("--status", action="store_true", help="Show this role's service status")
        actions.add_argument("--check", action="store_true", help="Check prerequisites without starting")
    model.add_argument("--type", dest="model_type", choices=("audio", "omni", "video"),
                       help="Model type; video is an alias for omni")
    model.add_argument("--model-path")
    model.add_argument("--ref-audio")
    model.add_argument("--memory-minutes", type=int)
    model.add_argument("--host", help="Listen address (default: 127.0.0.1; use 0.0.0.0 or the server's VPN address for other computers)")
    web.add_argument("--model-url", help=f"Model server URL, e.g. http://10.194.99.8:8031 (default: {DEFAULT_MODEL_URL})")
    web.add_argument("--harness-config", help="Override the referenced Harness JSON file")
    web.add_argument("--host")
    web.add_argument("--no-login", action="store_true",
                     help="Fail immediately if Codex is not authenticated")
    return parser


def parse_launch_options(root: Path, argv=None):
    launch_argv = list(sys.argv[1:] if argv is None else argv)
    caller = Path(os.getenv("VENUS_LAUNCH_CWD", str(Path.cwd()))).resolve()
    parser = _parser()
    args = parser.parse_args(launch_argv)
    # Status and shutdown must remain usable even if the deployment JSON was removed.
    if args.status or args.stop:
        return parser, args, launch_argv
    path = Path(_absolute(args.config, caller)) if args.config else root / DEFAULT_CONFIG[args.role]
    load = load_model_options if args.role == "model" else load_web_options
    options = load(path) if args.config or path.exists() else {}

    def resolve(cli_value, env_names, config_name, default=None, *, is_path=False):
        if cli_value is not None:
            value = cli_value
        else:
            value = next((os.environ[k] for k in env_names if os.environ.get(k)), None)
            if value is None:
                # Config paths were already resolved relative to the JSON file.
                return options.get(config_name) if options.get(config_name) not in (None, "") else default
        return _absolute(value, caller) if is_path and value else value

    def numbers(specs):
        for attr, env, key, default, cast in specs:
            try:
                value = cast(resolve(getattr(args, attr), (env,), key, default))
            except (ValueError, TypeError):
                parser.error(f"Invalid {key}")
            if attr == "port" and not 1 <= value <= 65535:
                parser.error(f"{key} must be between 1 and 65535")
            if attr in ("memory_minutes", "startup_timeout") and (not math.isfinite(value) or value <= 0):
                parser.error(f"{key} must be positive and finite")
            if attr == "host" and not value.strip():
                parser.error("host must not be empty")
            setattr(args, attr, value)

    # The background child runs from the project root. Freeze resolved arguments
    # so a caller-relative config/asset path cannot change meaning after detach.
    if args.role == "model":
        mode = args.model_type or os.getenv("VENUS_MODEL_TYPE") or options.get("type", "omni")
        if mode not in ("audio", "video", "omni"):
            parser.error("model type must be audio, video or omni")
        args.model_type = "omni" if mode == "video" else mode
        args.model_path = resolve(args.model_path,
            (f"{args.model_type.upper()}_MODEL_PATH", "MODEL_PATH"), "path", is_path=True)
        args.ref_audio = resolve(args.ref_audio, ("REF_AUDIO",), "reference_audio", is_path=True)
        numbers((
            ("host", "VENUS_MODEL_HOST", "host", "127.0.0.1", str),
            ("port", "VENUS_MODEL_PORT", "port", 8031, int),
            ("memory_minutes", "VENUS_MEMORY_MINUTES", "memory_minutes", 40, int),
            ("startup_timeout", "VENUS_STARTUP_TIMEOUT", "startup_timeout", 600, float),
        ))
        child = ["model", "--type", args.model_type, "--host", args.host, "--port", str(args.port),
                 "--memory-minutes", str(args.memory_minutes),
                 "--startup-timeout", str(args.startup_timeout)]
        if args.model_path:
            child += ["--model-path", args.model_path]
        if args.ref_audio:
            child += ["--ref-audio", args.ref_audio]
    else:
        args.model_url = resolve(args.model_url, ("VENUS_MODEL_URL",), "model_url", DEFAULT_MODEL_URL)
        if not http_url(args.model_url):
            parser.error(f"Model URL must be http(s)://host:port, got {args.model_url!r}")
        args.harness_config = resolve(args.harness_config, ("HARNESS_CONFIG",), "harness_config", is_path=True)
        if not args.harness_config:
            parser.error(f"Set harness.config in {path} (or --harness-config / HARNESS_CONFIG); see the Harness README")
        if not Path(args.harness_config).is_file():
            parser.error(f"Harness config does not exist: {args.harness_config}; set harness.config to an existing Harness configuration file; see the Harness README")
        args.demo_config = path if path.exists() else None
        numbers((
            ("port", "VENUS_WEB_PORT", "web_port", 8032, int),
            ("host", "VENUS_WEB_HOST", "web_host", "127.0.0.1", str),
            ("startup_timeout", "VENUS_STARTUP_TIMEOUT", "startup_timeout", 600, float),
        ))
        child = ["web", "--model-url", args.model_url, "--harness-config", args.harness_config,
                 "--port", str(args.port), "--host", args.host,
                 "--startup-timeout", str(args.startup_timeout)]
    if args.config or path.exists():
        child += ["--config", str(path)]
    if getattr(args, "no_login", False):
        child.append("--no-login")
    return parser, args, child
