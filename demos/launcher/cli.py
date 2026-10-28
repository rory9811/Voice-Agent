"""Launch, health-check and stop one Realtime-Venus role: model (GPU server) or web (local)."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import checked_file, ensure_port_available, local_url
from .supervisor import StackSupervisor, read_state
from .variants import select_checkpoint
from .options import parse_launch_options
from demos.model.settings import model_token, require_token
from demos.variants import model_name


def setup_backend(harness_path: Path, demo_path: Path | None, runtime: Path, allow_login: bool) -> None:
    from demos.settings import UserSetup
    from demos.server.configuration import WebConfiguration

    configuration = WebConfiguration(harness_path, demo_path=demo_path)
    setup = configuration.load()
    settings_changed = False
    if not harness_path.exists():
        setup.data["workspace"] = ""
        settings_changed = True

    configured_binary = setup.general.command[0]
    candidates = (
        os.getenv("GENERAL_CODEX_BINARY"),
        configured_binary,
        shutil.which("codex"),
        str(runtime / "bin/codex"),
    )
    binary = next(
        (resolved for candidate in candidates if candidate and (resolved := shutil.which(candidate))),
        None,
    )
    if not binary:
        raise RuntimeError("Codex executable is missing. Run bash install.sh web.")
    if binary != configured_binary:
        setup.data["codex"]["command"] = [binary, *setup.general.command[1:]]
        settings_changed = True

    if settings_changed:
        # Reconstruct derived settings after filling or repairing startup defaults.
        setup = UserSetup(setup.data, harness_path)
    check = setup.check(web=False)
    if not check["ok"]:
        raise RuntimeError("; ".join(check["problems"]))
    binary = setup.general.command[0]
    status = subprocess.run(
        [binary, "login", "status"], capture_output=True, timeout=30
    )
    if status.returncode:
        if not allow_login or not sys.stdin.isatty():
            raise RuntimeError(
                f"Codex login required. Run {binary} login --device-auth, then retry."
            )
        print("Complete Codex device login in your browser.", flush=True)
        subprocess.run([binary, "login", "--device-auth"], check=True)
    if setup.data["workspace"]:
        setup.workspace.mkdir(parents=True, exist_ok=True)
    if settings_changed:
        configuration.save_startup(setup.data)


def prepare_model(root: Path, args):
    model = select_checkpoint(root, args.model_type, explicit=args.model_path)
    ref = checked_file(Path(args.ref_audio) if args.ref_audio else model / "assets/HT_ref_audio.wav")
    require_token(args.host, model_token())
    ensure_port_available(args.host, args.port)
    python = os.getenv("VENUS_MODEL_PYTHON", sys.executable)
    subprocess.run(
        [
            python,
            "-c",
            'import torch; assert torch.cuda.is_available(), "No CUDA GPU available"; print("GPU:", torch.cuda.get_device_name(0))',
        ],
        check=True,
        timeout=60,
    )
    command = [
        python, "-m", "demos.model.server",
        "--model-type", args.model_type,
        "--model-path", str(model),
        "--ref-audio", str(ref),
        "--memory-minutes", str(args.memory_minutes),
        "--host", args.host,
        "--port", str(args.port),
    ]

    def show():
        print(f"\n{model_name(args.model_type)} API listening on {args.host}:{args.port}", flush=True)
        if args.host in ("127.0.0.1", "localhost"):
            print("Only this server can reach it. To serve your computer, set host in model_server.json "
                  "(0.0.0.0 or this server's VPN address) and VENUS_MODEL_TOKEN.", flush=True)
        else:
            address = "<this server's VPN address>" if args.host == "0.0.0.0" else args.host
            print(f"On your computer, set model.url in config.json to http://{address}:{args.port}, set "
                  "VENUS_MODEL_TOKEN to the same secret, then run: bash start.sh web", flush=True)
        print("Logs: runtime/logs/model.log; stop: bash start.sh model --stop", flush=True)

    state = {"model_type": args.model_type, "model_path": str(model), "port": args.port}
    print(f"Loading {model_name(args.model_type)}...", flush=True)
    return command, local_url(args.host, args.port, "/healthz"), state, show


def prepare_web(root: Path, args):
    from demos.server import resources

    runtime = root / "runtime"
    ensure_port_available(args.host, args.port)
    setup_backend(Path(args.harness_config), args.demo_config, runtime, allow_login=not args.no_login)
    # Reachability, readiness and the shared token, before the web service starts.
    resources.check_model(args.model_url, model_token())
    command = [
        sys.executable, "-m", "demos.server.cli",
        "--model-server", args.model_url,
        "--config", args.harness_config,
        "--host", args.host,
        "--port", str(args.port),
    ]
    if args.demo_config:
        command += ["--demo-config", str(args.demo_config)]

    def show():
        print(f"\nRealtime-Venus: http://localhost:{args.port}", flush=True)
        print(f"Model server: {args.model_url}", flush=True)
        print("Logs: runtime/logs/web.log; stop: bash start.sh web --stop", flush=True)

    state = {"model_url": args.model_url, "port": args.port}
    return command, local_url(args.host, args.port, "/health"), state, show


def _run(argv=None):
    root = Path(os.getenv("VENUS_ROOT", Path.cwd())).expanduser().resolve()
    parser, args, child_argv = parse_launch_options(root, argv)
    runtime = root / "runtime"
    runtime.mkdir(exist_ok=True)
    state_path = runtime / f"{args.role}.json"
    state = read_state(state_path)
    if args.status:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return
    if args.stop:
        if state["alive"]:
            os.kill(state["pid"], signal.SIGTERM)
            deadline = time.monotonic() + 50
            while read_state(state_path)["alive"] and time.monotonic() < deadline:
                time.sleep(0.2)
            if read_state(state_path)["alive"]:
                raise RuntimeError(
                    "Shutdown is still in progress; inspect runtime/logs before retrying."
                )
        print(f"Realtime-Venus {args.role} service stopped.")
        return
    if state["alive"]:
        parser.error(
            f"This checkout already runs the {args.role} service. Use {args.role} --status or --stop."
        )
    prepare = prepare_model if args.role == "model" else prepare_web
    command, health_url, info, show = prepare(root, args)
    if args.check:
        print(f"{args.role} prerequisites: OK")
        return
    if args.detach:
        (runtime / "logs").mkdir(exist_ok=True)
        child_args = [a for a in child_argv if a != "--detach"]
        if args.role == "web" and "--no-login" not in child_args:
            child_args.append("--no-login")
        with (runtime / "logs" / f"{args.role}-launcher.log").open("ab", buffering=0) as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "demos", *child_args],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + args.startup_timeout + 90
        try:
            while child.poll() is None and time.monotonic() < deadline:
                current = read_state(state_path)
                if current.get("pid") == child.pid and current.get("status") == "ready":
                    show()
                    return
                time.sleep(0.25)
            raise RuntimeError(
                f"Startup failed; see runtime/logs/{args.role}-launcher.log"
            )
        except BaseException:
            child.terminate()
            child.wait(timeout=50)
            raise
    with (runtime / f"{args.role}.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error(f"Another launcher owns the {args.role} service in this checkout")
        StackSupervisor(runtime, args.role, command, health_url, args.startup_timeout, info).run(show)


def main(argv=None):
    try:
        return _run(argv)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
