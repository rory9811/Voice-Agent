"""Claude Code login check: ready only on a Claude subscription login, never an API key."""
import json
import subprocess

from harness.llm.claude import ISOLATION_ARGS, cli_env

from .codex_login import CodexLoginCheck

LOGIN = "docker compose exec -it web claude auth login"


class ClaudeLoginCheck(CodexLoginCheck):
    MESSAGES = {
        "logged_in": "Claude Code is logged in with a Claude subscription / Claude Code 已用订阅登录",
        "not_logged_in": f"Claude Code is not logged in. In deploy/web run {LOGIN}, then recheck / Claude Code 未登录，请在 deploy/web 目录运行 {LOGIN}，然后重新检查",
        "not_subscription": f"Claude Code is logged in without a Claude subscription. In deploy/web run {LOGIN} and sign in with your Claude account / Claude Code 当前不是订阅登录，请在 deploy/web 目录运行 {LOGIN}，用 Claude 账号登录",
        "missing": "Claude Code executable not found / 找不到 Claude Code 程序",
        "timeout": "Claude Code login check timed out. Recheck after confirming Claude Code works on this computer / Claude Code 登录检查超时，请确认本机的 Claude Code 能正常运行后重新检查",
        "error": "Could not verify Claude Code login. Check Claude Code on this computer and retry / 无法确认 Claude Code 登录状态，请检查本机的 Claude Code 后重试",
    }

    def _state(self, resolved):
        # Same scrubbed environment and settings isolation as every task run.
        result = subprocess.run(
            [resolved, *ISOLATION_ARGS, "auth", "status", "--json"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=cli_env(),
            timeout=5, check=False, text=True,
        )
        try:
            status = json.loads(result.stdout)
        except ValueError:
            return "error"
        if result.returncode or not isinstance(status, dict):
            return "error"
        if not status.get("loggedIn"):
            return "not_logged_in"
        return "logged_in" if status.get("authMethod") == "claude.ai" else "not_subscription"
