"""Bootstrap the complete stack without changing the system Python environment."""

from __future__ import annotations

import argparse
import fcntl
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
CODEX_VERSION = "0.153.4"


def run(command, **kwargs):
    subprocess.run([str(part) for part in command], check=True, **kwargs)


def install_codex():
    existing = os.getenv("GENERAL_CODEX_BINARY") or shutil.which("codex")
    local = RUNTIME / "bin/codex"
    if existing:
        run([existing, "--version"])
        return
    if local.is_file():
        run([local, "--version"])
        return
    architecture = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(platform.machine())
    if not architecture:
        raise RuntimeError(
            "The automatic Codex installer supports Linux x86_64 and aarch64."
        )
    name = f"codex-{architecture}-unknown-linux-musl"
    url = os.getenv(
        "CODEX_DOWNLOAD_URL",
        f"https://github.com/openai/codex/releases/download/rust-v{CODEX_VERSION}/{name}.tar.gz",
    )
    local.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=RUNTIME) as directory:
        archive = Path(directory) / "codex.tar.gz"
        with (
            urllib.request.urlopen(url, timeout=120) as response,
            archive.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
        with tarfile.open(archive) as package:
            member = next(
                (m for m in package if m.isfile() and Path(m.name).name == name), None
            )
            if member is None:
                raise RuntimeError(
                    "Codex archive does not contain the expected executable."
                )
            temporary = Path(directory) / "codex"
            with package.extractfile(member) as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output)
            temporary.chmod(0o755)
            run([temporary, "--version"])
            os.replace(temporary, local)


def preferred_python():
    explicit = os.getenv("VENUS_INSTALL_PYTHON")
    if explicit:
        return explicit
    for candidate in [
        shutil.which("python3.11"),
        shutil.which("python3.12"),
        sys.executable,
        *sorted((Path(sys.prefix) / "envs").glob("*/bin/python")),
    ]:
        if not candidate or not Path(candidate).is_file():
            continue
        result = subprocess.run(
            [
                candidate,
                "-c",
                "import sys; sys.exit(not ((3,11) <= sys.version_info[:2] < (3,13)))",
            ],
            capture_output=True,
            timeout=15,
        )
        if result.returncode == 0:
            return candidate
    return "3.11"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        default=preferred_python(),
        help="Python executable or version for the isolated environment",
    )
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error(
            "Run the server installer on Linux; use any modern browser on your local computer."
        )
    RUNTIME.mkdir(exist_ok=True)
    with (RUNTIME / "install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        bootstrap = RUNTIME / "bootstrap/bin/python"
        print("Preparing the isolated installer...", flush=True)
        if not bootstrap.exists():
            run([sys.executable, "-m", "venv", RUNTIME / "bootstrap"])
        run(
            [
                bootstrap,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "uv==0.12.15",
            ]
        )
        uv = RUNTIME / "bootstrap/bin/uv"
        env = dict(
            os.environ,
            UV_CACHE_DIR=os.getenv("UV_CACHE_DIR", str(RUNTIME / "cache")),
            UV_PYTHON_INSTALL_DIR=str(RUNTIME / "python"),
            UV_LINK_MODE="copy",
            UV_SYSTEM_CERTS=os.getenv("UV_SYSTEM_CERTS", "true"),
            UV_HTTP_TIMEOUT=os.getenv("UV_HTTP_TIMEOUT", "300"),
            UV_CONCURRENT_DOWNLOADS=os.getenv("UV_CONCURRENT_DOWNLOADS", "4"),
        )
        if "PIP_INDEX_URL" in env and "UV_INDEX_URL" not in env:
            env["UV_INDEX_URL"] = env["PIP_INDEX_URL"]
        for pip_key, uv_key in [
            ("index-url", "UV_INDEX_URL"),
            ("trusted-host", "UV_INSECURE_HOST"),
        ]:
            if uv_key not in env:
                configured = subprocess.run(
                    [bootstrap, "-m", "pip", "config", "get", f"global.{pip_key}"],
                    capture_output=True,
                    text=True,
                )
                if configured.returncode == 0 and configured.stdout.strip():
                    env[uv_key] = configured.stdout.strip()
        python = RUNTIME / "venv/bin/python"
        if not python.exists():
            run([uv, "venv", "--python", args.python, RUNTIME / "venv"], env=env)
        run(
            [
                python,
                "-c",
                'import sys; assert (3,11) <= sys.version_info[:2] < (3,13), "Python 3.11 or 3.12 required"',
            ]
        )
        install = [uv, "pip", "install", "--python", python]
        print("Installing model and application dependencies...", flush=True)
        run([*install, "-r", ROOT / "demos/requirements.txt"], env=env)
        run(
            [
                *install,
                "--no-deps",
                "--no-build-isolation",
                "--editable",
                ROOT,
            ],
            env=env,
        )
        install_codex()
        run(
            [
                python,
                "-c",
                'import demos.server.app; import demos.model.server; print("Application imports: OK")',
            ],
            cwd=ROOT,
        )
        run(
            [
                python,
                "-c",
                'import torch, torchaudio, transformers, stepaudio2; print("Model dependencies: OK; CUDA:", torch.cuda.is_available())',
            ]
        )
        print(
            "\nInstalled. Put your checkpoint in model_weight/ and run: bash start.sh"
        )
        print(
            "Existing Codex authentication is reused; first startup can guide device login."
        )


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Installation failed: {exc}", file=sys.stderr)
        sys.exit(1)
