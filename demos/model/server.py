"""HTTP and WebSocket API for the Realtime-Venus-Omni duplex model."""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.requests import HTTPConnection
from fastapi.responses import JSONResponse
from fastapi.websockets import WebSocketState

from demos.model import wire
from demos.model.adapter import AdapterError, ModelNotLoaded, RealtimeVenusOmniAdapter, _Session
from demos.model.checkpoint import checked_file, checkpoint_directory
from demos.model.prompts import REALTIME_VENUS_SYSTEM_PROMPT
from demos.model.settings import TOKEN_ENV, model_token, require_token
from demos.variants import model_name

_LOG = logging.getLogger("demos.model.server")

# Data files only: the web side loads them with the stock Qwen2 fast tokenizer,
# so no checkpoint code is shipped to (or executed on) the client.
TOKENIZER_FILES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "added_tokens.json",
    "vocab.json",
    "merges.txt",
)


@dataclass
class _ServerConfig:
    model_path: str | None = None
    ref_audio_path: str | None = None
    prompt_wav_path: str | None = None
    memory_minutes: int = 40
    model_type: str = "omni"
    model_revision: str = "realtime-venus-omni-v1"
    model_name: str = "Realtime-Venus-Omni"
    system_prompt: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"
    token: str | None = None


async def _build_adapter(config: _ServerConfig) -> RealtimeVenusOmniAdapter:
    return await RealtimeVenusOmniAdapter.load_real(
        config.model_path,
        memory_minutes=config.memory_minutes,
        model_type=config.model_type,
        ref_audio_path=config.ref_audio_path,
        prompt_wav_path=config.prompt_wav_path or config.ref_audio_path,
        model_revision=config.model_revision,
        model_name=config.model_name,
        system_prompt=config.system_prompt,
    )


def _error(code: str, message: str, ack: int) -> bytes:
    return wire.encode_frame({"type": "error", "code": code, "error": message, "ack": ack})


async def _receive(ws: WebSocket) -> tuple[dict[str, Any], bytes]:
    message = await ws.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    if message.get("bytes") is None:
        raise AdapterError("frames must be binary")
    try:
        return wire.decode_frame(message["bytes"])
    except wire.FrameError as exc:
        raise AdapterError(str(exc)) from exc


async def _read(
    ws: WebSocket, adapter: RealtimeVenusOmniAdapter, sess: _Session
) -> AdapterError | None:
    """Apply client frames until the peer leaves (None) or breaks the protocol."""
    try:
        while True:
            header, payload = await _receive(ws)
            adapter.receive(sess, header, payload)
    except WebSocketDisconnect:
        return None
    except AdapterError as exc:
        return exc


async def _write(
    ws: WebSocket, sess: _Session, link: asyncio.Event, sent: int, reader: asyncio.Task
) -> int | None:
    """Send the outbox after ``sent``; the close code once ``link`` is set.

    None once ``reader`` has ended.
    """
    outbox = sess.outbox
    while not reader.done():
        if link.is_set() and sess.link is not link:
            return 4001  # a resume moved the session to a newer connection
        if link.is_set() and sess.failure is not None:
            await ws.send_bytes(_error("model_error", sess.failure, sess.inbox.last))
            return 1011
        frames = outbox.after(sent)
        if frames:
            header, payload = frames[0]
            # A copy: the outbox keeps the header for a replay on a later connection.
            await ws.send_bytes(wire.encode_frame({**header, "ack": sess.inbox.last}, payload))
            sent = header["seq"]
        elif link.is_set():
            return 1000  # closed, and every frame up to ``closed`` is written
        else:
            outbox.ready.clear()
            waits = [asyncio.ensure_future(outbox.ready.wait()), asyncio.ensure_future(link.wait())]
            try:
                await asyncio.wait([*waits, reader], return_when=asyncio.FIRST_COMPLETED)
            finally:
                for wait in waits:
                    wait.cancel()
    return None


async def _serve_session(ws: WebSocket, adapter: RealtimeVenusOmniAdapter) -> None:
    link = asyncio.Event()  # the adapter sets it when this connection must stop
    try:
        header, _ = await _receive(ws)
        if header["type"] == "open":
            sess, reply = await adapter.open_session(header, link)
            sent = 0
        elif header["type"] == "resume":
            sess = await adapter.resume_session(
                header.get("session_id"),
                header.get("incarnation"),
                ack=header.get("ack"),
                consumed=header.get("consumed"),
                link=link,
            )
            reply, sent = {"type": "resumed"}, header["ack"]
        else:
            raise AdapterError(f"the first frame must be open or resume, got {header['type']!r}")
    except WebSocketDisconnect:
        raise
    except AdapterError as exc:
        await ws.send_bytes(_error(exc.code, str(exc), 0))  # no session is attached yet
        await ws.close(1011)
        return
    except Exception as exc:  # noqa: BLE001 - e.g. prepare ran out of GPU memory
        _LOG.exception("opening the session failed")
        await ws.send_bytes(_error("model_error", f"{type(exc).__name__}: {exc}", 0))
        await ws.close(1011)
        return
    try:
        await ws.send_bytes(wire.encode_frame({**reply, "ack": sess.inbox.last}))
        # Only this coroutine sends; the reader task only receives.
        reader = asyncio.create_task(_read(ws, adapter, sess))
        try:
            code = await _write(ws, sess, link, sent, reader)
        except WebSocketDisconnect:
            code = None  # the peer is gone; the reader may have met a bad frame first
        finally:
            reader.cancel()
        if code is not None:
            await ws.close(code)
        elif reader.done() and (exc := reader.result()) is not None:
            # Starlette refuses any send once the writer has met the peer's disconnect.
            live = ws.application_state is WebSocketState.CONNECTED
            try:
                if live:
                    await ws.send_bytes(_error(exc.code, str(exc), sess.inbox.last))
            finally:
                # Even if the peer is gone: a broken session must never be resumed.
                await adapter.close_session(sess, "protocol_error")
            if live:
                await ws.close(1011)
    finally:
        adapter.detach(sess, link)


def create_app(config: _ServerConfig) -> FastAPI:
    """Construct the FastAPI app; the adapter is loaded in the lifespan."""

    state: dict[str, Any] = {"adapter": None, "ready": False}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        adapter = await _build_adapter(config)
        app.state.adapter = adapter
        state["adapter"] = adapter
        state["ready"] = True
        _LOG.info("adapter ready (revision=%s)", adapter._model_revision)  # noqa: SLF001
        try:
            yield
        finally:
            # Best-effort: close an open session on shutdown.
            sess = getattr(adapter, "_session", None)  # noqa: SLF001
            if sess is not None and not sess.closed:
                try:
                    await adapter.close_session(sess, "server_shutdown")
                except Exception:  # noqa: BLE001
                    _LOG.warning("shutdown close failed", exc_info=True)

    app = FastAPI(
        title="demos.model Realtime-Venus-Omni ServingPort",
        version="0.1.0",
        lifespan=lifespan,
    )

    def token_ok(request: HTTPConnection) -> bool:
        if config.token is None:
            return True
        supplied = request.headers.get("authorization", "").encode()
        return secrets.compare_digest(supplied, f"Bearer {config.token}".encode())

    @app.middleware("http")
    async def require_bearer(request: Request, call_next):
        # /healthz stays public for liveness probes; it reveals no session.
        if request.url.path != "/healthz" and not token_ok(request):
            return JSONResponse(
                status_code=401,
                content={"error": f"missing or wrong {TOKEN_ENV}", "type": "Unauthorized"},
            )
        return await call_next(request)

    def adapter() -> RealtimeVenusOmniAdapter:
        a = state["adapter"]
        if a is None:
            raise ModelNotLoaded("adapter is not ready")
        return a

    @app.get("/healthz")
    async def health(request: Request):
        if not state["ready"]:
            return JSONResponse(status_code=503, content={"ready": False})
        a = adapter()
        h = await a.health()
        return {
            "ready": True,
            "model_type": config.model_type,
            "model": config.model_name,
            "token_ok": token_ok(request),
            "protocol_version": wire.PROTOCOL_VERSION,
            **h,
        }

    @app.websocket("/session")
    async def session(ws: WebSocket):
        # The http middleware does not see WebSockets. Accept first: a close
        # before accept reaches the client as an HTTP 403, not as code 1008.
        await ws.accept()
        try:
            if not token_ok(ws):
                await ws.close(1008)
                return
            await _serve_session(ws, adapter())
        except WebSocketDisconnect:
            pass  # the peer left; a session it held waits for a resume

    @app.get("/stats")
    async def stats():
        # Capacity measurements; token-protected because they reveal activity.
        return await adapter().stats()

    @app.get("/tokenizer")
    async def tokenizer():
        root = Path(config.model_path)
        return {
            "files": {
                name: (root / name).read_text(encoding="utf-8")
                for name in TOKENIZER_FILES
                if (root / name).is_file()
            }
        }

    return app


def _parse_args(argv: list[str] | None = None) -> _ServerConfig:
    p = argparse.ArgumentParser(
        description="FastAPI Realtime-Venus-Omni VenusOmni ServingPort server"
    )
    p.add_argument(
        "--model-path", default=None, help="path to model_weight/ (trust_remote_code)"
    )
    p.add_argument("--ref-audio", default=None,
                   help="Reference voice wav; relative paths are inside the checkpoint (default: assets/HT_ref_audio.wav)")
    p.add_argument(
        "--prompt-wav", default=None, help="optional prompt_wav_path for token2wav init"
    )
    p.add_argument("--memory-minutes", type=int, default=40)
    p.add_argument("--model-type", choices=("audio", "omni"), default="omni")
    p.add_argument("--model-revision", default=None)
    p.add_argument("--model-name", default=None)
    p.add_argument(
        "--system-prompt",
        default=None,
        help="duplex system-prompt text (default: Streaming Omni Conversation.)",
    )
    p.add_argument(
        "--system-prompt-file",
        default=None,
        help="read system prompt from a UTF-8 file (overrides --system-prompt)",
    )
    p.add_argument(
        "--system-prompt-model-default",
        action="store_true",
        help="use the model's built-in DEFAULT_SYSTEM_PROMPT (Chinese-only) instead of the streaming default",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--log-level", default="info")
    a = p.parse_args(argv)
    token = model_token()
    try:
        require_token(a.host, token)
    except ValueError as exc:
        p.error(str(exc))
    if a.system_prompt_model_default:
        system_prompt = None
    elif a.system_prompt_file:
        system_prompt = Path(a.system_prompt_file).read_text(encoding="utf-8")
    elif a.system_prompt is not None:
        system_prompt = a.system_prompt
    else:
        system_prompt = REALTIME_VENUS_SYSTEM_PROMPT
    return _ServerConfig(
        model_path=a.model_path,
        ref_audio_path=a.ref_audio,
        prompt_wav_path=a.prompt_wav,
        memory_minutes=a.memory_minutes,
        model_type=a.model_type,
        model_revision=a.model_revision or f"realtime-venus-{a.model_type}-v1",
        model_name=a.model_name or model_name(a.model_type),
        system_prompt=system_prompt,
        host=a.host,
        port=a.port,
        log_level=a.log_level,
        token=token,
    )


def resolve_assets(config: _ServerConfig) -> _ServerConfig:
    """Validate the checkpoint and pick its reference voice before loading anything."""
    if not config.model_path:
        raise ValueError("Set the model path (--model-path)")
    model = checkpoint_directory(Path(config.model_path), config.model_type)
    # A relative reference voice lives inside the checkpoint directory.
    ref = Path(config.ref_audio_path or "assets/HT_ref_audio.wav")
    ref = checked_file(ref if ref.is_absolute() else model / ref)
    return replace(config, model_path=str(model), ref_audio_path=str(ref))


def main(argv: list[str] | None = None) -> None:
    import uvicorn

    try:
        config = resolve_assets(_parse_args(argv))
    except ValueError as exc:
        raise SystemExit(f"Startup failed: {exc}") from None
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_level=config.log_level,
        ws_ping_interval=5.0,
        ws_ping_timeout=5.0,
        ws_max_size=wire.MAX_FRAME_BYTES,
        # "auto" picks the legacy websockets implementation, deprecated since websockets 14.
        ws="websockets-sansio",
    )


if __name__ == "__main__":
    main()
