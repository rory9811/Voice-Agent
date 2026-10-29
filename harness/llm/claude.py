"""Claude Code CLI on the user's subscription login: one `claude -p` process per call.

Validated with Claude Code 2.1.309 (stream-json in and out, --json-schema structured output).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
import subprocess
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

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
            rejected = verified = False
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
                    verified = True
                elif kind != "system" and not verified:
                    # Fail closed: nothing counts until init has shown the subscription login.
                    raise ClaudeCliError(
                        "Claude Code reported no subscription login before working; refusing to run / "
                        "Claude Code 开始工作前未确认订阅登录，已拒绝运行",
                        code="authentication_error",
                    )
                if kind == "rate_limit_event":
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
        """SIGTERM every process group the CLI started, SIGKILL after 3 s; True once all are gone.

        Tool commands run detached in their own groups (`bwrap --new-session`), invisible from
        the CLI's group, so a live CLI is frozen first and its descendants' groups recorded.
        """
        process = self.process
        if process is None:
            return True
        groups = {process.pid}
        frozen = False
        if process.returncode is None:
            try:
                os.kill(process.pid, signal.SIGSTOP)
                frozen = True
            except ProcessLookupError:
                pass
            groups |= _descendant_groups(process.pid)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for group in groups:
                try:
                    os.killpg(group, sig)
                except (ProcessLookupError, PermissionError):
                    pass
            if frozen:
                # A stopped CLI handles its SIGTERM (and session shutdown) only once resumed.
                with contextlib.suppress(ProcessLookupError):
                    os.kill(process.pid, signal.SIGCONT)
            deadline = loop.time() + 3
            while _any_alive(groups) and loop.time() < deadline:
                await asyncio.sleep(0.05)
            if not _any_alive(groups):
                break
        return not _any_alive(groups)

    async def _drain_stderr(self):
        while line := await self.process.stderr.readline():
            self.stderr_tail.append(line.decode(errors="replace").strip()[:1000])


def _any_alive(groups):
    for group in groups:
        try:
            os.killpg(group, 0)
        except (ProcessLookupError, PermissionError):
            continue
        return True
    return False


def _descendant_groups(root):
    """Process groups of root's live descendants, from /proc (Linux) or ps (macOS)."""
    parents = {}
    if Path("/proc/self/stat").exists():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text()
            except OSError:
                continue
            # "pid (comm) state ppid pgrp ..."; comm may itself contain spaces or parentheses.
            fields = stat[stat.rindex(")") + 2 :].split()
            parents[int(entry.name)] = (int(fields[1]), int(fields[2]))
    else:
        listing = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True, check=True
        ).stdout
        for line in listing.splitlines():
            pid, ppid, pgid = map(int, line.split())
            parents[pid] = (ppid, pgid)
    groups, seen, frontier = set(), {root}, [root]
    while frontier:
        parent = frontier.pop()
        for pid, (ppid, pgid) in parents.items():
            if ppid == parent and pid not in seen:
                seen.add(pid)
                groups.add(pgid)
                frontier.append(pid)
    return groups


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
