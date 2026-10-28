from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Protocol

from ..config import Settings


class PlannerBackend(Protocol):
    """Transport boundary for one stateless planner decision."""

    async def complete(self, instructions: str, context: dict[str, Any]) -> str: ...


class CodexPlannerBackend:
    """Codex CLI backend that reuses the user's local Codex authentication."""

    def __init__(self, settings: Settings) -> None:
        self._executable = settings.codex_path
        self._model = settings.codex_model
        self._effort = settings.codex_effort
        self._workspace = settings.codex_workspace
        self._timeout_seconds = settings.codex_timeout_seconds

    def model_options(self) -> list[str]:
        """Override global CLI defaults for every model call, including images."""
        options = ["--config", f"model_reasoning_effort={json.dumps(self._effort)}"]
        if self._model:
            options.extend(["--model", self._model])
        return options

    async def complete(self, instructions: str, context: dict[str, Any]) -> str:
        prompt = (
            f"{instructions}\n\n"
            "你正在一个只读的规划后端中工作。不要读取文件、运行命令、修改工作区或调用外部工具；"
            "只根据下面的输入产生约定的 JSON object。\n\n"
            f"{_planner_request(context)}"
        )
        with tempfile.TemporaryDirectory(prefix="venus-model-call-") as temp:
            temp_path = Path(temp)
            workspace = (
                Path(self._workspace).expanduser() if self._workspace else temp_path
            )
            if self._workspace and not workspace.is_dir():
                raise RuntimeError(f"Codex 规划工作区不存在: {workspace}")
            output_path = temp_path / "last-message.json"
            command = [
                self._executable,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "--output-last-message",
                str(output_path),
                "--cd",
                str(workspace),
            ]
            command.extend(self.model_options())
            command.append("-")
            await self._run(command, prompt)
            if not output_path.is_file():
                raise RuntimeError("Codex 未生成最终规划结果")
            return output_path.read_text(encoding="utf-8").strip()

    async def _run(self, command: list[str], prompt: str) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"未找到 Codex CLI: {self._executable}；请先安装并登录 Codex，或设置 CODEX_PATH"
            ) from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")),
                timeout=self._timeout_seconds,
            )
        except TimeoutError as exc:
            await _stop_process(process)
            raise RuntimeError(
                f"Codex 规划超时（{self._timeout_seconds:g} 秒）"
            ) from exc
        except asyncio.CancelledError:
            await _stop_process(process)
            raise
        if process.returncode:
            detail = stderr.decode("utf-8", errors="replace").strip()
            if not detail:
                detail = stdout.decode("utf-8", errors="replace").strip()
            suffix = f"：{detail[-1200:]}" if detail else ""
            raise RuntimeError(f"Codex 规划失败（exit={process.returncode}）{suffix}")


def create_planner_backend(settings: Settings) -> PlannerBackend:
    return CodexPlannerBackend(settings)


def _planner_request(context: dict[str, Any]) -> str:
    return "请按约定 JSON 格式决策：\n" + json.dumps(context, ensure_ascii=False)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()


__all__ = [
    "CodexPlannerBackend",
    "PlannerBackend",
    "create_planner_backend",
]
