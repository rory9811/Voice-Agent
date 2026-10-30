"""Browser access to local task settings; no credentials are returned to the UI."""

import copy
import hashlib
import secrets
import tempfile

from demos.settings import UserSetup, config_path, load_setup, save_setup
from .claude_login import ClaudeLoginCheck
from .codex_login import CodexLoginCheck


class WebConfiguration:
    def __init__(self, path):
        self.path = config_path(path)
        self.token = secrets.token_urlsafe(32)
        self.logins = {"codex": CodexLoginCheck(), "claude": ClaudeLoginCheck()}

    def revision(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else "new"

    def load(self):
        return load_setup(self.path)

    def problems(self, setup):
        problems = setup.check()["problems"]
        # The service runs in a container: only the data folder reaches the user's disk.
        if not setup.workspace.is_relative_to(self.path.parent):
            problems.append(
                "Choose a task workspace inside the data folder, for example workspace / "
                "请填写数据目录内的任务目录，例如 workspace"
            )
        return problems

    def check(self, setup, *, force_login=False):
        result = {"problems": self.problems(setup)}
        login = self.logins[setup.general.agent].check(setup.general.command[0], force=force_login)
        if login["state"] != "logged_in" and login["message"] not in result["problems"]:
            result["problems"].append(login["message"])
        return {"ok": not result["problems"], "problems": result["problems"], "agent_login": login}

    def agent_login(self, agent):
        """Login state of any agent, so the page can answer before it is saved."""
        if agent not in self.logins:
            raise ValueError("未知的任务执行器")
        return self.logins[agent].check(self.load().data[agent]["command"][0], force=True)

    def status(self, *, force_login=False):
        try:
            return self.path.exists() and self.check(self.load(), force_login=force_login)["ok"]
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def public(self):
        setup = self.load()
        result = self.check(setup, force_login=True)
        public_data = copy.deepcopy(setup.data)
        public_data["gemini"]["api_key"] = ""
        return {
            "data": public_data,
            "gemini_key_configured": bool(setup.gemini.resolved_key()),
            "revision": self.revision(),
            "token": self.token,
            "configured": self.path.exists() and result["ok"],
            "check": result,
            "agent_login": result["agent_login"],
        }

    def save(self, body):
        if not isinstance(body, dict) or body.get("revision") != self.revision():
            raise RuntimeError(
                "配置已变化，请重新打开"
            )
        data = copy.deepcopy(body.get("data"))
        if not isinstance(data, dict):
            raise ValueError("配置无效")
        current = self.load()
        gemini = data.setdefault("gemini", {})
        if not isinstance(gemini, dict):
            raise ValueError("Gemini 配置无效")
        # A blank write-only field means retain the stored key; never return it.
        if gemini.get("api_key", "") == "":
            gemini["api_key"] = current.gemini.api_key
        setup = UserSetup(data, self.path)
        problems = self.problems(setup)
        result = {"ok": not problems, "problems": problems}
        if problems:
            return result
        setup.workspace.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=setup.workspace):
            pass
        save_setup(setup.data, self.path)
        return result
