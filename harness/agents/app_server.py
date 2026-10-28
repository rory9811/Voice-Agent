"""Owned stdio JSON-RPC connection; no prompt replay after a transport failure."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable


class AppServerError(RuntimeError):
    def __init__(self, error):
        self.status_code = 0
        self.code = ""
        if isinstance(error, dict):
            data = error.get("data") or {}
            if isinstance(data, dict):
                self.status_code = data.get(
                    "status_code", data.get("httpStatusCode", 0)
                )
                self.code = data.get("code", "")
        super().__init__(str(error))


class AppServer:
    def __init__(
        self,
        command: tuple[str, ...],
        cwd: str,
        on_message: Callable[[dict], Awaitable[None]],
        timeout: float = 30,
    ):
        self.command, self.cwd, self.on_message, self.timeout = (
            command,
            cwd,
            on_message,
            timeout,
        )
        self.process = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._reader = self._stderr = None
        self._handlers: set[asyncio.Task] = set()
        self.stderr_tail = deque(maxlen=20)
        self.failure = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
            limit=16 * 1024 * 1024,
        )
        self._reader = asyncio.create_task(self._read())
        self._stderr = asyncio.create_task(self._drain_stderr())
        try:
            await self.request(
                "initialize",
                {
                    "clientInfo": {"name": "venus_general", "version": "1.0.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await self.send({"method": "initialized"})
        except BaseException:
            await self.close()
            raise

    async def request(self, method: str, params: dict, timeout: float | None = None):
        if self.failure:
            raise AppServerError(self.failure)
        self._next_id += 1
        rpc_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[rpc_id] = future
        try:
            await self.send({"id": rpc_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout or self.timeout)
        finally:
            self._pending.pop(rpc_id, None)

    async def send(self, message: dict):
        async with self._write_lock:
            if not self.process or self.process.returncode is not None:
                raise AppServerError("Codex app-server is not running")
            self.process.stdin.write(
                (json.dumps(message, ensure_ascii=False) + "\n").encode()
            )
            await self.process.stdin.drain()

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if "method" not in message and "id" in message:
                    future = self._pending.get(message["id"])
                    if future is not None and not future.done():
                        if "error" in message:
                            future.set_exception(AppServerError(message["error"]))
                        else:
                            future.set_result(message.get("result", {}))
                elif "id" in message:
                    # A question may remain open for minutes. Never block the reader.
                    task = asyncio.create_task(self._handle_request(message))
                    self._handlers.add(task)
                    task.add_done_callback(self._handlers.discard)
                else:
                    await self.on_message(message)
            raise AppServerError(
                "Codex app-server disconnected; execution was not replayed"
            )
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 - fail all RPC waiters at the transport boundary
            self.failure = str(exc)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(AppServerError(self.failure))
            await self.on_message(
                {"method": "transport/failed", "params": {"error": self.failure}}
            )

    async def _handle_request(self, message):
        try:
            await self.on_message(message)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - fail all RPC waiters at the transport boundary
            try:
                await self.send(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32603,
                            "message": str(exc),
                        },
                    }
                )
            except (AppServerError, ConnectionError):
                pass

    async def _drain_stderr(self):
        while line := await self.process.stderr.readline():
            self.stderr_tail.append(line.decode(errors="replace").strip()[:1000])

    async def close(self):
        process = self.process
        if process is not None:
            try:
                if os.name == "posix":
                    # The parent may have exited while a child still owns its pipes.
                    os.killpg(process.pid, signal.SIGTERM)
                elif process.returncode is None:
                    process.terminate()
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                await process.wait()
            except ProcessLookupError:
                pass
        tasks = [
            t
            for t in (self._reader, self._stderr, *self._handlers)
            if t is not None and t is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AppServerError("Codex app-server closed"))
