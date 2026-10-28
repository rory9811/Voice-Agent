"""Bounded local credential checks; never expose command output or credentials."""
import shutil
import subprocess
import threading
import time

MESSAGES = {
    "logged_in": "Codex is logged in / Codex 已登录",
    "not_logged_in": "Codex is not logged in. Run codex login --device-auth on the server, then recheck / Codex 未登录，请在服务器运行 codex login --device-auth，然后重新检查",
    "missing": "Codex executable not found / 找不到 Codex 程序",
    "timeout": "Codex login check timed out. Recheck after confirming Codex works on the server / Codex 登录检查超时，请确认服务器上的 Codex 能正常运行后重新检查",
    "error": "Could not verify Codex login. Check Codex on the server and retry / 无法确认 Codex 登录状态，请检查服务器上的 Codex 后重试",
}

class CodexLoginCheck:
    def __init__(self):
        self._lock = threading.Lock()
        self._cached = None

    def check(self, binary, *, force=False):
        resolved = shutil.which(binary)
        if not resolved:
            return {"state": "missing", "message": MESSAGES["missing"]}
        with self._lock:
            if not force and self._cached:
                key, checked_at, result = self._cached
                if key == resolved and time.monotonic() - checked_at < 15:
                    return dict(result)
            try:
                result = subprocess.run(
                    [resolved, "login", "status"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5, check=False,
                )
                state = "logged_in" if result.returncode == 0 else "not_logged_in" if result.returncode == 1 else "error"
            except subprocess.TimeoutExpired:
                state = "timeout"
            except OSError:
                state = "error"
            result = {"state": state, "message": MESSAGES[state]}
            self._cached = (resolved, time.monotonic(), result)
            return dict(result)
