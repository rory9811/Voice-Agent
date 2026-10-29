"""Test helpers for the model link: a real model server and a lossy TCP proxy.

Not collected by pytest (no ``test_`` prefix); imported by the link tests.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import uvicorn

from demos.model import wire
from demos.model.server import _ServerConfig, create_app


@asynccontextmanager
async def serve_model(adapter, *, token=None):
    """Serve ``create_app`` around ``adapter`` on a free port in this event loop."""
    config = uvicorn.Config(
        create_app(_ServerConfig(token=token)),
        host="127.0.0.1",
        port=0,
        log_level="warning",
        ws_ping_interval=5.0,
        ws_ping_timeout=5.0,
        ws_max_size=wire.MAX_FRAME_BYTES,
        # The legacy implementation "auto" picks warns on import with websockets >= 14.
        ws="websockets-sansio",
    )
    server = uvicorn.Server(config)
    with patch("demos.model.server._build_adapter", AsyncMock(return_value=adapter)):
        task = asyncio.create_task(server.serve())
        try:
            while not server.started:
                if task.done():
                    task.result()
                    raise RuntimeError("model server stopped during startup")
                await asyncio.sleep(0.01)
            port = server.servers[0].sockets[0].getsockname()[1]
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            await task


class LatencyProxy:
    """Forwards TCP to ``target_port``, each direction in order and ``one_way_ms`` late."""

    def __init__(self, target_port: int, one_way_ms: float = 0) -> None:
        self._target_port = target_port
        self._delay_s = one_way_ms / 1000
        self._refuse_until = 0.0
        self._transports: set[asyncio.Transport] = set()
        self._held: set[asyncio.Transport] = set()
        self._tasks: set[asyncio.Task] = set()
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    def drop_all(self) -> None:
        """Kill every live connection without a close handshake, as aTrust does."""
        for transport in list(self._transports):
            transport.abort()

    def hold(self) -> None:
        """Forward nothing more, not even a close, on the live connections: a link gone silent."""
        self._held.update(self._transports)

    def refuse_for(self, seconds: float) -> None:
        """Close new connections right after accepting them for ``seconds``."""
        self._refuse_until = asyncio.get_running_loop().time() + seconds

    async def close(self) -> None:
        self._server.close()
        self.drop_all()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._server.wait_closed()

    async def _accept(self, client_reader, client_writer) -> None:
        task = asyncio.current_task()
        self._tasks.add(task)
        try:
            if asyncio.get_running_loop().time() < self._refuse_until:
                client_writer.transport.abort()
                return
            try:
                server_reader, server_writer = await asyncio.open_connection(
                    "127.0.0.1", self._target_port
                )
            except OSError:
                client_writer.transport.abort()
                return
            pair = (client_writer.transport, server_writer.transport)
            self._transports.update(pair)
            try:
                await asyncio.gather(
                    self._pipe(client_reader, server_writer),
                    self._pipe(server_reader, client_writer),
                )
            finally:
                for transport in pair:
                    transport.abort()
                    self._transports.discard(transport)
        finally:
            self._tasks.discard(task)

    async def _pipe(self, reader, writer) -> None:
        loop = asyncio.get_running_loop()
        chunks: asyncio.Queue = asyncio.Queue()

        async def deliver():
            while (item := await chunks.get()) is not None:
                due, data = item
                await asyncio.sleep(max(0.0, due - loop.time()))
                if writer.transport not in self._held:
                    writer.write(data)
                    await writer.drain()
            if writer.transport not in self._held:
                writer.write_eof()

        delivery = asyncio.create_task(deliver())
        try:
            while data := await reader.read(1 << 16):
                chunks.put_nowait((loop.time() + self._delay_s, data))
            chunks.put_nowait(None)
            await delivery
        except (ConnectionError, OSError):
            pass  # dropped: the peer's transport was aborted
        finally:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
