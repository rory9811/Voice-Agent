"""Own one service process per role and record its state for --status/--stop."""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path



def process_start(pid: int) -> str | None:
    """Process start time prevents acting on a recycled PID.

    Linux reads kernel start ticks (GPU containers often lack ``ps``); macOS has
    no /proc, so it asks ``ps`` with a fixed locale and timezone, keeping the
    value identical across shells.
    """
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return None if fields[0] == "Z" else fields[19]
        except (OSError, IndexError):
            return None
    try:
        result = subprocess.run(
            ["ps", "-o", "stat=,lstart=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    status, _, started = result.stdout.strip().partition(" ")
    return None if not started or status.startswith("Z") else started.strip()


def read_state(path: Path) -> dict:
    try:
        state = json.loads(path.read_text())
        state["alive"] = (
            bool(state.get("started"))
            and process_start(state["pid"]) == state["started"]
        )
        return state
    except (OSError, ValueError, KeyError, TypeError):
        return {"alive": False, "status": "stopped"}


def write_state(path: Path, data: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


# Health probes target this machine's own service; never a proxy.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def healthy(url: str) -> bool:
    try:
        with _DIRECT.open(url, timeout=2) as response:
            body = json.load(response)
            return response.status == 200 and (
                body.get("ready") is True or body.get("status") == "ok"
            )
    except (OSError, ValueError, urllib.error.URLError):
        return False


class StackSupervisor:
    """Own one service process (model or web) and clean it up on failure or shutdown."""

    def __init__(
        self,
        runtime: Path,
        name: str,
        command: list[str],
        health_url: str,
        startup_timeout: float,
        state: dict,
    ):
        self.runtime = runtime
        self.name = name
        self.command = command
        self.health_url = health_url
        self.startup_timeout = startup_timeout
        self.child: subprocess.Popen | None = None
        self.stopping = False
        self.state = {
            "pid": os.getpid(),
            "started": process_start(os.getpid()),
            "status": "starting",
            **state,
        }

    def spawn(self) -> None:
        env = dict(os.environ)
        env["PATH"] = str(self.runtime / "bin") + os.pathsep + env.get("PATH", "")
        env.setdefault("OMP_NUM_THREADS", "1")
        env.setdefault("MKL_NUM_THREADS", "1")
        env.setdefault("MAX_NUM_FRAMES", "100000")
        with (self.runtime / "logs" / f"{self.name}.log").open("ab", buffering=0) as log:
            self.child = subprocess.Popen(
                self.command,
                cwd=self.runtime.parent,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.state["child_pid"] = self.child.pid
        self.save()

    def save(self) -> None:
        write_state(self.runtime / f"{self.name}.json", self.state)

    def check_child(self) -> None:
        if self.child is not None and self.child.poll() is not None:
            raise RuntimeError(
                f"{self.name} exited with code {self.child.returncode}; see runtime/logs/{self.name}.log"
            )

    def wait_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while not self.stopping:
            self.check_child()
            if healthy(self.health_url):
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Service startup timed out: {self.health_url}")
            time.sleep(0.25)
        raise InterruptedError("Startup cancelled")

    def shutdown(self) -> None:
        child = self.child
        if child is None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)

    def run(self, ready_callback=None) -> None:
        def stop(_signal, _frame):
            self.stopping = True

        previous = {
            sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)
        }
        (self.runtime / "logs").mkdir(parents=True, exist_ok=True)
        self.save()
        try:
            self.spawn()
            self.wait_ready()
            self.state["status"] = "ready"
            self.save()
            if ready_callback:
                ready_callback()
            while not self.stopping:
                self.check_child()
                time.sleep(0.25)
        except BaseException as exc:
            self.state["error"] = str(exc)
            if not isinstance(exc, InterruptedError):
                raise
        finally:
            self.shutdown()
            self.state["status"] = "stopped"
            self.save()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
