"""Claude Code CLI on the user's subscription login: one `claude -p` process per call.

Validated with Claude Code 2.1.309 (stream-json in and out, --json-schema structured output).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping

from harness.core.models import BackendResponse

from .backends import _planner_request

# Any of these makes Claude Code bill the API or another provider instead of the subscription.
_BILLING_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
_BILLING_NAMES = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})
# Settings files can set apiKeyHelper; claude.ai MCP connectors are not part of a task.
# Global options: they must precede any subcommand such as `auth status`.
ISOLATION_ARGS = ("--setting-sources", "", "--strict-mcp-config")
PRINT_ARGS = (
    "-p",
    "--output-format",
    "stream-json",
    "--verbose",
    "--input-format",
    "stream-json",
)
# Callers each need at least one field; an empty object fails the CLI's own validation.
ANY_OBJECT = json.dumps({"type": "object", "minProperties": 1})


def cli_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    return {
        key: value
        for key, value in environ.items()
        if not key.startswith(_BILLING_PREFIXES) and key not in _BILLING_NAMES
    }


def model_args(model: str | None, effort: str | None) -> list[str]:
    return [*(["--model", model] if model else []), *(["--effort", effort] if effort else [])]


def user_message(content: list[dict]) -> bytes:
    message = {"type": "user", "message": {"role": "user", "content": content}}
    return (json.dumps(message, ensure_ascii=False) + "\n").encode()


class ClaudeCliError(RuntimeError):
    """code/status_code feed harness.jobs.faults.classify_fault; the text goes to logs only."""

    def __init__(self, message: str, *, code: str = "", status_code: int = 0):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class CliRun:
    """One print-mode process in its own process group; anything but the subscription is refused."""

    def __init__(self, command: list[str], *, cwd: str):
        self.command = command
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None
        self.stderr_tail = deque(maxlen=40)

    @property
    def pid(self):
        return self.process.pid if self.process else None

    async def run(
        self,
        stdin: bytes,
        on_message: Callable[[dict], Awaitable[None]] | None = None,
    ) -> dict:
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            env=cli_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=16 * 1024 * 1024,
        )
        drain = asyncio.create_task(self._drain_stderr())
        try:
            self.process.stdin.write(stdin)
            await self.process.stdin.drain()
            self.process.stdin.close()
            rejected = False
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    raise ClaudeCliError("Claude Code printed a non-JSON line") from None
                kind = message.get("type")
                if kind == "system" and message.get("subtype") == "init":
                    source = message.get("apiKeySource")
                    if source != "none":
                        raise ClaudeCliError(
                            f"Claude Code is not using the subscription login (credential source: {source}); "
                            f"refusing to run / Claude Code 当前未使用订阅登录（凭据来源：{source}），已拒绝运行",
                            code="authentication_error",
                        )
                elif kind == "rate_limit_event":
                    info = message.get("rate_limit_info") or {}
                    if info.get("isUsingOverage") is True:
                        raise ClaudeCliError(
                            "Claude subscription usage is exhausted; refusing paid extra usage / "
                            "Claude 订阅额度已用完，不使用按量付费的额外用量",
                            code="quota_exceeded",
                        )
                    rejected = rejected or info.get("status") == "rejected"
                if on_message is not None:
                    await on_message(message)
                if kind == "result":
                    value = _result_value(message, rejected)
                    try:
                        # Let it persist the session for --resume before the group is swept.
                        await asyncio.wait_for(self.process.wait(), 10)
                    except TimeoutError:
                        pass
                    return value
            await self.process.wait()
            detail = "\n".join(self.stderr_tail)[-1200:]
            raise ClaudeCliError(
                f"Claude Code exited (exit={self.process.returncode}) without a result: {detail}"
            )
        finally:
            await self.close()
            drain.cancel()

    async def close(self) -> bool:
        """SIGTERM the whole group, SIGKILL after 3 s; True once no process of it remains."""
        process = self.process
        if process is None:
            return True
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            # asyncio reaps the leader meanwhile; tool children are swept with the group.
            deadline = loop.time() + 3
            while self._group_alive() and loop.time() < deadline:
                await asyncio.sleep(0.05)
            if not self._group_alive():
                break
        return not self._group_alive()

    def _group_alive(self):
        try:
            os.killpg(self.process.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    async def _drain_stderr(self):
        while line := await self.process.stderr.readline():
            self.stderr_tail.append(line.decode(errors="replace").strip()[:1000])


def _result_value(message, rejected):
    if message.get("is_error") or message.get("subtype") != "success":
        raise ClaudeCliError(
            str(message.get("result") or message.get("subtype")),
            code="quota_exceeded" if rejected else "",
            status_code=int(message.get("api_error_status") or 0),
        )
    value = message.get("structured_output")
    if not isinstance(value, dict):
        raise ClaudeCliError("Claude Code returned no structured result")
    return value


class ClaudeModelBackend:
    """Stateless model calls: no tools, no session, our instructions as the whole system prompt."""

    provider_name = "claude-code"

    def __init__(self, *, executable: str, model: str | None, effort: str | None, timeout_s: float):
        self._executable = executable
        self._model = model
        self._effort = effort
        self.timeout_s = timeout_s
        self.model_name = model or "claude-configured"

    async def generate(self, instructions: str, content: list[dict]) -> str:
        with tempfile.TemporaryDirectory(prefix="venus-claude-call-") as temp:
            command = [
                self._executable,
                *PRINT_ARGS,
                *ISOLATION_ARGS,
                "--tools",
                "",
                "--no-session-persistence",
                "--system-prompt",
                instructions,
                "--json-schema",
                ANY_OBJECT,
                *model_args(self._model, self._effort),
            ]
            async with asyncio.timeout(self.timeout_s):
                value = await CliRun(command, cwd=temp).run(user_message(content))
        return json.dumps(value, ensure_ascii=False)

    async def complete(self, instructions: str, context: dict) -> str:
        return await self.generate(
            instructions, [{"type": "text", "text": _planner_request(context)}]
        )


class ClaudeDirect:
    """Image-aware direct answers; audio is not supported, like the Codex path."""

    def __init__(self, backend: ClaudeModelBackend):
        self.backend = backend

    async def execute(self, request, context):
        started = time.monotonic()
        frames = context.video.frames if context.video else ()
        if context.snapshot.audio_chunks and not frames:
            raise ValueError(
                "This backend accepts text and images; ask Venus to describe the audio in its request"
            )
        content = [{"type": "text", "text": f"回答语言：{request.language}\n用户要求：{request.query}"}]
        content.extend(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": frame.mime_type,
                    "data": base64.b64encode(frame.data).decode("ascii"),
                },
            }
            for frame in frames
        )
        raw = await self.backend.generate(
            "根据用户要求和附加图片直接回答，不运行命令、不读取其他文件、不调用工具。"
            '返回 JSON {"speech":"回答内容"}，不加 Markdown 包裹。',
            content,
        )
        speech = json.loads(raw).get("speech")
        if not isinstance(speech, str) or not speech.strip():
            raise ValueError("Direct 模型返回了空文本")
        return BackendResponse(
            speech,
            self.backend.provider_name,
            self.backend.model_name,
            int((time.monotonic() - started) * 1000),
        )

    async def aclose(self):
        pass
