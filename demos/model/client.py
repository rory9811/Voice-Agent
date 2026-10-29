"""Web-side ``VenusOmniServingPort`` over one WebSocket to the model server.

Runs in the Venus harness environment (Python >=3.11).  Frames follow
:mod:`demos.model.wire`: a JSON header named after the DTO fields in
``harness/bridge/serving.py`` plus the raw bytes as payload.  Client frames
that must arrive (inputs, prefill, playback acks, close) wait in an
:class:`~demos.model.wire.Outbox` until the server's ``ack`` confirms them.
When the link drops, the port reconnects, resumes the same model session and
replays what the other side has not confirmed, so the host sees every event
exactly once and in order.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import fields
from enum import Enum
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from harness.bridge.serving import (
    AudioAppend,
    CloseSession,
    GeneratedAudio,
    InputAccepted,
    ModelOutputStep,
    OpenSession,
    PlaybackAck,
    PrefillAppend,
    PrefillApplied,
    ServingEvent,
    SessionCapabilities,
    SessionClosed,
    SessionOpened,
    VideoFrameAppend,
    VideoSegmentAppend,
)

from . import wire
from .settings import TOKEN_ENV

_LOG = logging.getLogger("demos.model.client")

RECONNECT_DEADLINE_S = 45.0
# Waits before each reconnect attempt; the last one repeats.
RECONNECT_DELAYS_S = (0.5, 1.0, 2.0)
# How long a close waits for the model while the link is down.
CLOSE_TIMEOUT_S = 10.0

# A failed connect or a dropped connection; retried while the session can resume.
_LINK_ERRORS = (OSError, WebSocketException)
_EVENTS = {"input_accepted": InputAccepted, "output": ModelOutputStep, "prefill_applied": PrefillApplied}


class RemoteServingError(RuntimeError):
    """The model session cannot continue.  Never a ``TimeoutError``: the host
    reads that as "no output yet"."""


class RemoteSessionClosed(RemoteServingError):
    pass


class RemoteModelUnavailable(RemoteServingError):
    """The model cannot serve this client; ``code`` tells the browser why."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        # model_busy | model_mismatch | model_unreachable | model_link_lost
        self.code = code


def _header(kind: str, request: Any) -> dict[str, Any]:
    """The request's fields except its bytes, which travel as the payload."""
    header: dict[str, Any] = {"type": kind}
    for field in fields(request):
        if field.name != "data":
            value = getattr(request, field.name)
            header[field.name] = value.value if isinstance(value, Enum) else value
    return header


def _dto(cls: type, header: dict[str, Any], **override: Any) -> Any:
    values = {f.name: header[f.name] for f in fields(cls) if f.name in header}
    return cls(**{**values, **override})


def _model_error(header: dict[str, Any]) -> RemoteServingError:
    code, message = header.get("code"), str(header.get("error"))
    if code in ("model_busy", "model_mismatch"):
        return RemoteModelUnavailable(code, message)
    if code == "session_lost":
        return RemoteModelUnavailable("model_link_lost", message)
    return RemoteServingError(message)


def _rejected(exc: ConnectionClosed) -> None:
    if exc.rcvd is not None and exc.rcvd.code == 1008:
        raise RemoteServingError(f"model server rejected {TOKEN_ENV}") from None


class RemoteOmniServingPort:
    """``VenusOmniServingPort`` backed by the model server's ``WS /session``."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 60.0,
        length_penalty: float | None = None,
        token: str | None = None,
    ) -> None:
        if length_penalty is not None:
            from .settings import DuplexSettings

            DuplexSettings(length_penalty=length_penalty)
        self._length_penalty = length_penalty
        base = base_url.rstrip("/")
        self._url = base.replace("http", "ws", 1) + "/session"
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout_s = timeout_s  # bounds the open and close handshakes
        self.link_up = False
        self.on_link_change: Callable[[bool], Awaitable[None]] | None = None
        self._outbox = wire.Outbox()
        self._inbox = wire.Inbox()
        self._consumed = 0  # outputs next_event has returned
        self._state_due = False
        self._events: asyncio.Queue[ServingEvent | None] = asyncio.Queue()
        self._end: RemoteServingError | None = None  # what next_event and send_* raise
        self._closed: asyncio.Future[SessionClosed] | None = None
        self._finished = False  # the server confirmed the close
        self._resume: dict[str, Any] = {}
        self._task: asyncio.Task | None = None
        self._step_gaps, self._speech_gaps = wire.StepStats(), wire.StepStats()
        self._last_output: float | None = None
        self._last_speech: tuple[str, float] | None = None

    # -- the port -----------------------------------------------------------

    async def open_session(self, request: OpenSession) -> SessionOpened:
        first = {
            "type": "open",
            "protocol_version": request.protocol_version,
            "session_id": request.session_id,
            "model": request.model,
            "ack": 0,
            "consumed": 0,
        }
        if self._length_penalty is not None:
            first["length_penalty"] = self._length_penalty
        try:
            async with asyncio.timeout(self._timeout_s):
                ws, header = await self._handshake(first)
        except _LINK_ERRORS as exc:
            raise RemoteModelUnavailable(
                "model_unreachable", f"model server unreachable at {self._url}: {exc}"
            ) from exc
        try:
            if header["type"] != "opened":
                raise wire.FrameError(f"expected opened, got {header['type']!r}")
            opened = _dto(
                SessionOpened,
                header,
                capabilities=SessionCapabilities(**header["capabilities"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            await ws.close()
            raise RemoteServingError(f"model server sent a bad opened frame: {exc}") from exc
        self._resume = {"session_id": opened.session_id, "incarnation": opened.incarnation}
        self.link_up = True
        self._task = asyncio.create_task(self._run(ws, 0), name=f"model-link:{opened.session_id}")
        return opened

    def send_audio(self, request: AudioAppend) -> None:
        self._send(_header("audio", request), request.data)

    def send_video_frame(self, request: VideoFrameAppend) -> None:
        self._send(_header("video_frame", request), request.data)

    def send_video_segment(self, request: VideoSegmentAppend) -> None:
        raise ValueError("this model server does not accept video segments")

    def send_prefill(self, request: PrefillAppend) -> None:
        self._send(_header("prefill", request))

    def send_playback_ack(self, request: PlaybackAck) -> None:
        self._send(_header("playback_ack", request))

    async def next_event(self, *, timeout_s: float | None = None) -> ServingEvent:
        if self._end is None:
            async with asyncio.timeout(timeout_s):
                event = await self._events.get()
        if self._end is not None:
            raise self._end
        if isinstance(event, ModelOutputStep):
            self._consumed += 1
            self._state_due = True  # tell the model it may step again
            self._outbox.ready.set()
        return event

    async def close_session(self, request: CloseSession) -> SessionClosed:
        if self._end is not None:
            raise self._end
        self._closed = asyncio.get_running_loop().create_future()
        self._send(_header("close", request))
        self._stop(RemoteSessionClosed("the model session is closed"))
        limit = self._timeout_s if self.link_up else CLOSE_TIMEOUT_S
        try:
            return await asyncio.wait_for(self._closed, limit)
        except TimeoutError:
            if self._task is not None:
                self._task.cancel()
            raise RemoteServingError(
                f"the model server did not confirm the close within {limit:g} s"
            ) from None

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        _LOG.info(
            "model output gaps: steps %s speech %s",
            self._step_gaps.summary(),
            self._speech_gaps.summary(),
        )

    # -- session state ------------------------------------------------------

    def _send(self, header: dict[str, Any], payload: bytes = b"") -> None:
        if self._end is not None:
            raise self._end
        try:
            self._outbox.push(header, payload)
        except OverflowError:
            exc = RemoteModelUnavailable(
                "model_link_lost", "the model server stopped confirming what this side sent"
            )
            self._fail(exc)
            if self._task is not None:
                self._task.cancel()
            raise exc from None

    def _stop(self, exc: RemoteServingError) -> None:
        """From now on next_event and send_* raise ``exc``; the first end wins."""
        if self._end is None:
            self._end = exc
            self._events.put_nowait(None)  # wakes a blocked next_event

    def _fail(self, exc: RemoteServingError) -> None:
        self._stop(exc)
        if self._closed is not None and not self._closed.done():
            self._closed.set_exception(exc)

    async def _set_link(self, up: bool) -> None:
        self.link_up = up
        _LOG.info("model link %s", "resumed" if up else "lost; reconnecting")
        if self.on_link_change is not None:
            try:
                await self.on_link_change(up)
            except Exception:  # noqa: BLE001 - a UI failure must not stop the link
                _LOG.warning("on_link_change(%s) failed", up, exc_info=True)

    # -- connection management ----------------------------------------------

    async def _handshake(self, first: dict[str, Any]) -> tuple[ClientConnection, dict[str, Any]]:
        """Connect and send ``first``; the server's reply header."""
        ws = await connect(
            self._url,
            additional_headers=self._headers,
            proxy=None,  # the model URL is a direct VPN/LAN endpoint, never a proxy target
            open_timeout=10,
            ping_interval=5,
            ping_timeout=5,
            max_size=wire.MAX_FRAME_BYTES,
        )
        try:
            try:
                await ws.send(wire.encode_frame(first))
                data = await ws.recv()
            except ConnectionClosed as exc:
                _rejected(exc)
                raise
            header, _ = self._decode(data)
            return ws, header
        except BaseException:
            await ws.close()
            raise

    async def _run(self, ws: ClientConnection, sent: int) -> None:
        """Serve connections until the session ends; reconnect after a drop."""
        try:
            while True:
                try:
                    await self._pump(ws, sent)
                finally:
                    await ws.close()
                if self._finished:
                    return
                await self._set_link(False)
                ws, sent = await self._reconnect()
                await self._set_link(True)
        except RemoteServingError as exc:
            self._fail(exc)
        except Exception as exc:  # noqa: BLE001 - a dead link must not leave next_event waiting
            _LOG.exception("model link failed")
            self._fail(RemoteServingError(f"model link failed: {exc!r}"))

    async def _reconnect(self) -> tuple[ClientConnection, int]:
        """A resumed connection and the last seq the server has from us."""
        # A close in progress gives up as soon as a close during an outage would.
        deadline = CLOSE_TIMEOUT_S if self._closed is not None else RECONNECT_DEADLINE_S
        try:
            async with asyncio.timeout(deadline):
                for attempt in itertools.count():
                    await asyncio.sleep(RECONNECT_DELAYS_S[min(attempt, len(RECONNECT_DELAYS_S) - 1)])
                    first = {"type": "resume", **self._resume, "ack": self._inbox.last, "consumed": self._consumed}
                    try:
                        ws, header = await self._handshake(first)
                    except _LINK_ERRORS:
                        continue
                    if header["type"] != "resumed":
                        await ws.close()
                        raise self._protocol(wire.FrameError(f"expected resumed, got {header['type']!r}"))
                    return ws, header["ack"]
        except TimeoutError:
            pass
        raise RemoteModelUnavailable(
            "model_link_lost", f"the link to the model server was down for more than {deadline:g} s"
        )

    async def _pump(self, ws: ClientConnection, sent: int) -> None:
        """Run one connection until it ends; raises if the session cannot go on."""
        writer = asyncio.create_task(self._write(ws, sent))
        try:
            while True:
                try:
                    data = await ws.recv()
                except ConnectionClosed as exc:
                    _rejected(exc)
                    return
                self._receive(data)
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

    async def _write(self, ws: ClientConnection, sent: int) -> None:
        """Send the outbox after ``sent``, then a ``state`` whenever ``consumed`` grew."""
        while True:
            frames = self._outbox.after(sent)
            if frames:
                header, payload = frames[0]
                sent = header["seq"]
            elif self._state_due:
                header, payload = {"type": "state"}, b""
            else:
                self._outbox.ready.clear()
                await self._outbox.ready.wait()
                continue
            self._state_due = False  # every frame carries the latest consumed
            # A copy: the outbox keeps the header for a replay on a later connection.
            stamped = {**header, "ack": self._inbox.last, "consumed": self._consumed}
            await ws.send(wire.encode_frame(stamped, payload))

    # -- frames from the server ---------------------------------------------

    @staticmethod
    def _protocol(exc: Exception) -> RemoteServingError:
        return RemoteServingError(f"model server broke the link protocol: {exc}")

    def _decode(self, data: Any) -> tuple[dict[str, Any], bytes]:
        """Header and payload of a server frame whose ``ack`` is applied; raises on ``error``."""
        try:
            if not isinstance(data, bytes):
                raise wire.FrameError("frames must be binary")
            header, payload = wire.decode_frame(data)
            if header["type"] == "error":
                raise _model_error(header)
            ack = header.get("ack")
            if type(ack) is not int or ack > self._outbox.last_seq:
                raise wire.FrameError(f"bad ack {ack!r}")
            self._outbox.confirm(ack)
            return header, payload
        except wire.FrameError as exc:
            raise self._protocol(exc) from exc

    def _receive(self, data: Any) -> None:
        header, payload = self._decode(data)
        kind = header["type"]
        try:
            if kind != "closed" and kind not in _EVENTS:
                raise wire.FrameError(f"unexpected {kind!r} frame")
            seq = header.get("seq")
            if type(seq) is not int:
                raise wire.FrameError(f"bad seq {seq!r}")
            if not self._inbox.accept(seq):
                return  # replayed after a reconnect; already delivered
            if kind == "closed":
                closed = _dto(SessionClosed, header)
                self._finished = True
                self._stop(RemoteSessionClosed("the model session is closed"))
                if self._closed is not None and not self._closed.done():
                    self._closed.set_result(closed)
                return
            if kind == "output":
                audio = header["audio"]
                event = _dto(
                    ModelOutputStep,
                    header,
                    audio=None if audio is None else GeneratedAudio(data=payload, **audio),
                )
                self._record_arrival(event)
            else:
                event = _dto(_EVENTS[kind], header)
        except (KeyError, TypeError, ValueError) as exc:
            raise self._protocol(exc) from exc
        self._events.put_nowait(event)

    def _record_arrival(self, step: ModelOutputStep) -> None:
        now = time.monotonic()
        if self._last_output is not None:
            self._step_gaps.add((now - self._last_output) * 1000)
        self._last_output = now
        if step.audio is not None:
            if self._last_speech is not None and self._last_speech[0] == step.generation_id:
                self._speech_gaps.add((now - self._last_speech[1]) * 1000)
            self._last_speech = (step.generation_id, now)


__all__ = [
    "CLOSE_TIMEOUT_S",
    "RECONNECT_DEADLINE_S",
    "RECONNECT_DELAYS_S",
    "RemoteModelUnavailable",
    "RemoteOmniServingPort",
    "RemoteServingError",
    "RemoteSessionClosed",
]
