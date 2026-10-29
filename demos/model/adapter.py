"""Serialize a Realtime-Venus-Omni model session and expose the serving contract."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping

from demos.model import wire as sw
from demos.model.decoding import configure_length_penalty
from demos.model.settings import DuplexSettings
from demos.model.wire import Inbox, Outbox, StepStats

_LOG = logging.getLogger("demos.model.adapter")

# 12 protocol tokens (mirror harness.core.venus.VENUS_PROTOCOL_TOKENS; hardcoded
# so the server never imports the harness).
PROTOCOL_TOKENS = (
    "<|speak|>",
    "<|listen|>",
    "<|turn_eos|>",
    "<|chunk_eos|>",
    "<unit>",
    "</unit>",
    "<image>",
    "</image>",
    "<delegate>",
    "</delegate>",
    "<backend>",
    "</backend>",
)

# Outputs the client has not taken yet; the driver stops stepping at this many.
OUTPUT_WINDOW = 4
# A detached session waits this long for its client to resume it.
RESUME_WINDOW_S = 60.0

# ---------------------------------------------------------------------------
# Capacity measurements, served by server.py at /stats
# ---------------------------------------------------------------------------


def gpu_memory() -> dict[str, int] | None:
    """This process's and the whole device's GPU memory in MiB; None without CUDA."""
    try:
        import torch
    except ImportError:
        return None
    if not torch.cuda.is_available():
        return None
    mib = 1024 * 1024
    free, total = torch.cuda.mem_get_info()
    return {
        "allocated_mib": torch.cuda.memory_allocated() // mib,
        "reserved_mib": torch.cuda.memory_reserved() // mib,
        "peak_allocated_mib": torch.cuda.max_memory_allocated() // mib,
        "device_used_mib": (total - free) // mib,
        "device_total_mib": total // mib,
    }


def _reset_gpu_peak() -> None:
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()


# ---------------------------------------------------------------------------
# Errors -> the ``code`` the session socket reports to the client
# ---------------------------------------------------------------------------


class AdapterError(RuntimeError):
    code = "protocol"


class EventSeqMismatch(AdapterError):
    pass


class SessionBusy(AdapterError):
    code = "model_busy"


class ModelMismatch(AdapterError):
    code = "model_mismatch"


class Unsupported(AdapterError):
    pass


class SessionLost(AdapterError):
    code = "session_lost"


class ModelNotLoaded(AdapterError):
    code = "model_error"


# Client frames that carry a transport ``seq`` and are replayed after a reconnect.
_RELIABLE = frozenset({"audio", "video_frame", "prefill", "playback_ack", "close"})


def _checked(header: dict[str, Any], name: str, kind: type) -> Any:
    """``header[name]`` if it has exactly type ``kind`` (so a bool is not an int)."""
    value = header.get(name)
    if type(value) is not kind:
        raise AdapterError(
            f"{header.get('type')} frame needs {kind.__name__} {name!r}, got {value!r}"
        )
    return value


# ---------------------------------------------------------------------------
# Model backend protocol the adapter calls.  Real model = HuggingFaceMemoryDuplex;
# ---------------------------------------------------------------------------


def _official(model: Any) -> Any:
    """The object owning ``total_ids`` and the same-KV backend prefill."""
    return getattr(model, "official_duplex", model)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


class _Session:
    __slots__ = (
        "session_id",
        "incarnation",
        "model",
        "closed",
        "event_seq",
        "input_seq",
        "consumed_input_seq",
        "audio_buf",
        "frames",
        "audio_samples",
        "driver",
        "gen_epoch",
        "fg_gen",
        "resume_gen",
        "step_seq",
        "audio_seq",
        "prev_total_len",
        "ack_chunks",
        "prefill_attempts",
        "outbox",
        "inbox",
        "emitted",
        "consumed",
        "prefill_request",
        "link",
        "expiry",
        "failure",
    )

    def __init__(self, session_id: str, incarnation: int, model: str) -> None:
        self.session_id = session_id
        self.incarnation = incarnation
        self.model = model
        self.closed = False
        self.event_seq = 0
        self.input_seq = 0
        self.consumed_input_seq = 0
        self.audio_samples = 0  # count of 16k samples buffered
        self.audio_buf = bytearray()
        self.frames: list[bytes] = []
        self.driver: asyncio.Task | None = None
        self.gen_epoch = 0
        self.fg_gen: tuple[str, int] | None = None
        self.resume_gen: tuple[str, int] | None = None
        self.step_seq = 0
        self.audio_seq = 0
        self.prev_total_len = 0
        self.ack_chunks: dict[str, int] = {}
        # attempt_id -> (backend text, the prefill_applied header first sent)
        self.prefill_attempts: dict[str, tuple[str, dict[str, Any]]] = {}
        self.outbox = Outbox()
        self.inbox = Inbox()
        self.emitted = 0  # outputs pushed to the outbox
        self.consumed = 0  # outputs the client reports it has taken
        self.prefill_request: dict[str, str] | None = None
        self.link: asyncio.Event | None = None  # set() tells the attached connection to stop
        self.expiry: asyncio.Task | None = None
        self.failure: str | None = None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class RealtimeVenusOmniAdapter:
    """Serves one live duplex session on top of one model backend.

    Single session (``max_sessions=1``), matching the model's single KV cache.
    """

    def __init__(
        self,
        model: Any,
        *,
        special_token_ids: Mapping[str, int],
        model_revision: str,
        model_name: str = "Realtime-Venus-Omni",
        ref_audio: Any | None = None,
        prompt_wav_path: str | None = None,
        system_prompt: str | None = None,
        chunk_seconds: float = 1.0,
        sample_rate_hz: int = 16_000,
        max_new_speak_tokens_per_chunk: int = 50,
    ) -> None:
        self._model = model
        self._official = _official(model)
        self._needs_duplex = False
        self._special = dict(special_token_ids)
        self._model_revision = model_revision
        self._model_name = model_name
        self._ref_audio = ref_audio
        self._prompt_wav_path = prompt_wav_path
        # Custom duplex system prompt (identity).  None -> model's built-in
        # DEFAULT_SYSTEM_PROMPT (checkpoint model implementation:110).  Foreground prepare
        # forwards it via prefix_system_prompt (runtime.py:106 / engine
        # model_adapter.py:169 / official modeling.py:2795).
        self._system_prompt = system_prompt
        self._chunk_samples = int(sample_rate_hz * chunk_seconds)
        self._max_speak = int(max_new_speak_tokens_per_chunk)
        self._length_penalty = None
        self._decoding_implementation = "not_prepared"
        self._generation_options: dict[str, Any] = {}
        self._session: _Session | None = None
        self._open_lock = asyncio.Lock()
        self._closers: set[asyncio.Task] = set()  # keeps scheduled closes referenced
        self._steps = StepStats()
        self._memory_after_load: dict[str, int] | None = None
        self._wakeup = asyncio.Event()

    # -- construction -------------------------------------------------------

    @property
    def delegate_audio_filtered(self) -> bool:
        """Recognize the released model's span filter and early TTS flush.

        The current checkpoint exposes two session-state flags rather than a
        public capability attribute. Both must exist; legacy checkpoints keep
        the conservative Harness gate. An explicit model declaration takes
        precedence when a future checkpoint provides one.
        """
        declared = getattr(self._official, "delegate_audio_filtered", None)
        if declared is not None:
            return declared is True
        return (
            callable(getattr(self._official, "_convert_results_to_tts_input", None))
            and type(getattr(self._official, "_delegate_tts_skip", None)) is bool
            and type(getattr(self._official, "_delegate_turn_preflushed", None)) is bool
        )

    @classmethod
    async def load_real(
        cls,
        model_path: str,
        *,
        memory_minutes: int,
        ref_audio_path: str | None,
        prompt_wav_path: str | None = None,
        model_revision: str = "realtime-venus-omni-v1",
        model_name: str = "Realtime-Venus-Omni",
        system_prompt: str | None = None,
        chunk_seconds: float = 1.0,
        max_new_speak_tokens_per_chunk: int = 50,
        model_type: str = "omni",
    ) -> "RealtimeVenusOmniAdapter":
        model, tokenizer, ref_audio = await asyncio.to_thread(
            _load_real_blocking,
            model_path,
            memory_minutes,
            ref_audio_path,
            model_type,
        )
        special = {
            token: int(tokenizer.convert_tokens_to_ids(token))
            for token in PROTOCOL_TOKENS
        }
        missing = [t for t, v in special.items() if v is None or v < 0]
        if missing:
            raise ModelNotLoaded(
                "tokenizer missing protocol tokens: " + ", ".join(missing)
            )
        if len(set(special.values())) != len(special):
            raise ModelNotLoaded("protocol token ids are not unique")
        adapter = cls(
            model,
            special_token_ids=special,
            model_revision=model_revision,
            model_name=model_name,
            ref_audio=ref_audio,
            prompt_wav_path=prompt_wav_path,
            system_prompt=system_prompt,
            chunk_seconds=chunk_seconds,
            max_new_speak_tokens_per_chunk=max_new_speak_tokens_per_chunk,
        )
        # Weights plus static buffers: the part every instance repeats.
        adapter._memory_after_load = gpu_memory()
        return adapter

    # -- session lifecycle ----------------------------------------------------

    async def open_session(
        self, req: dict[str, Any], link: asyncio.Event
    ) -> tuple[_Session, dict[str, Any]]:
        protocol = req.get("protocol_version", sw.PROTOCOL_VERSION)
        if protocol != sw.PROTOCOL_VERSION:
            raise AdapterError(f"unsupported protocol_version: {protocol!r}")
        session_id = str(req["session_id"])
        model = str(req["model"])
        if model != self._model_name:
            raise ModelMismatch(
                f"model mismatch: server serves {self._model_name}, client requested {model}"
            )
        try:
            # Older non-browser clients keep the checkpoint's original behavior.
            length_penalty = DuplexSettings(
                length_penalty=req.get("length_penalty", 1.0)
            ).length_penalty
        except ValueError as exc:
            raise AdapterError(str(exc)) from exc
        async with self._open_lock:
            old = self._session
            if old is not None and not old.closed:
                if old.link is not None:
                    raise SessionBusy("a session is already open; max_sessions=1")
                # Its client is gone (e.g. the web server restarted): give up the slot.
                await self._close_locked(old, "replaced")
            incarnation = (self._session.incarnation + 1) if self._session else 1
            sess = _Session(session_id, incarnation, model)
            sess.link = link
            # The peak then measures this session alone.
            _reset_gpu_peak()
            # Re-prepare the model for this session (prepare is session-scoped,
            # single-use: reset_memory first if a previous session used it).
            await asyncio.to_thread(self._prepare_for_session, length_penalty)
            self._session = sess
            sess.driver = asyncio.create_task(
                self._driver_loop(sess), name=f"adapter-driver:{session_id}"
            )
        _LOG.info(
            "open_session %s incarnation=%d length_penalty=%s decoding=%s",
            session_id,
            incarnation,
            self._length_penalty,
            self._decoding_implementation,
        )
        return sess, sw.session_opened(
            session_id=session_id,
            incarnation=incarnation,
            model=model,
            model_revision=self._model_revision,
            special_token_ids=self._special,
            opened_at_ms=sw.now_ms(),
            video_segments=False,
            max_sessions=1,
            delegate_audio_filtered=self.delegate_audio_filtered,
        )

    def _prepare_for_session(self, length_penalty: float = 1.0) -> None:
        """Block in the model thread: reset + prepare a fresh duplex session."""

        if self._needs_duplex:
            self._model = self._model.as_duplex()
            self._official = _official(self._model)
            self._needs_duplex = False
        # If the runtime was already prepared by a previous session, reset first
        # so prepare() does not raise "already prepared" (runtime.py:104).
        runtime = getattr(self._model, "memory_runtime", None)
        if runtime is not None and hasattr(runtime, "reset_memory"):
            try:
                runtime.reset_memory()
            except Exception:  # noqa: BLE001 - reset may not be needed on a fresh load
                _LOG.debug("reset_memory skipped/failed", exc_info=True)
        self._model.prepare(
            prefix_system_prompt=self._system_prompt,
            ref_audio=self._ref_audio,
            prompt_wav_path=self._prompt_wav_path,
        )
        try:
            options, implementation = configure_length_penalty(
                self._official, length_penalty
            )
        except ValueError as exc:
            raise Unsupported(str(exc)) from exc
        self._generation_options = options
        self._length_penalty = length_penalty
        self._decoding_implementation = implementation

    async def resume_session(
        self,
        session_id: str,
        incarnation: int,
        *,
        ack: int,
        consumed: int,
        link: asyncio.Event,
    ) -> _Session:
        async with self._open_lock:
            sess = self._session
            if (
                sess is None
                or sess.closed
                or sess.session_id != session_id
                or sess.incarnation != incarnation
            ):
                raise SessionLost(f"no open session {session_id!r} incarnation {incarnation!r}")
            self._credit(sess, ack, consumed)
            if sess.link is not None:
                sess.link.set()  # a half-open connection the server still thinks is live
            if sess.expiry is not None:
                sess.expiry.cancel()
                sess.expiry = None
            sess.link = link
            self._wakeup.set()
        _LOG.info("resume_session %s incarnation=%d", session_id, incarnation)
        return sess

    def detach(self, sess: _Session, link: asyncio.Event) -> None:
        """The connection behind ``link`` ended; keep the session for a resume."""
        if sess.link is not link:
            return  # a newer connection already took the session over
        sess.link = None
        if not sess.closed:
            sess.expiry = asyncio.create_task(
                self._expire(sess), name=f"adapter-expiry:{sess.session_id}"
            )

    async def _expire(self, sess: _Session) -> None:
        await asyncio.sleep(RESUME_WINDOW_S)
        async with self._open_lock:
            # A resume holds this lock and would have cancelled us. Clear the
            # field so _close_locked does not cancel this very task.
            sess.expiry = None
            await self._close_locked(sess, "link_lost")

    def receive(self, sess: _Session, header: dict[str, Any], payload: bytes) -> None:
        """Apply one client frame. Never awaits: an input and its ack are one step."""
        if sess.closed:
            return  # frames still in flight at close have nothing left to act on
        self._credit(sess, header.get("ack"), header.get("consumed"))
        kind = header.get("type")
        if kind in _RELIABLE:
            try:
                if not sess.inbox.accept(_checked(header, "seq", int)):
                    return  # replayed after a reconnect; already applied
            except sw.FrameError as exc:
                raise AdapterError(str(exc)) from exc
        elif kind != "state":
            raise AdapterError(f"unknown frame type: {kind!r}")
        if kind == "audio":
            event_seq = _checked(header, "event_seq", int)
            if header.get("sample_rate_hz") != 16_000:
                raise AdapterError("input audio must be 16000 Hz")
            if len(payload) % 2:
                raise AdapterError("PCM16 audio needs an even number of bytes")
            self._fence_event(sess, event_seq)
            sess.audio_buf.extend(payload)
            sess.audio_samples += len(payload) // 2
            self._accept_input(sess, event_seq)
        elif kind == "video_frame":
            if self._model_name == "Realtime-Venus-Audio":
                raise Unsupported("The Audio checkpoint does not accept video frames")
            event_seq = _checked(header, "event_seq", int)
            self._fence_event(sess, event_seq)
            sess.frames.append(payload)
            self._accept_input(sess, event_seq)
        elif kind == "prefill":
            self._receive_prefill(sess, header)
        elif kind == "playback_ack":
            utterance_id = _checked(header, "utterance_id", str)
            cumulative = _checked(header, "cumulative_played_chunks", int)
            if cumulative < sess.ack_chunks.get(utterance_id, 0):
                raise AdapterError("playback acknowledgement moved backwards")
            sess.ack_chunks[utterance_id] = cumulative
        elif kind == "close":
            self._spawn_close(sess, "client_close")
        self._wakeup.set()

    @staticmethod
    def _credit(sess: _Session, ack: Any, consumed: Any) -> None:
        """Drop the frames the client confirmed and record the outputs it took."""
        for name, value in (("ack", ack), ("consumed", consumed)):
            if type(value) is not int:
                raise AdapterError(f"{name} must be an integer, got {value!r}")
        if ack > sess.outbox.last_seq or consumed > sess.emitted:
            raise AdapterError("ack or consumed is ahead of what the model sent")
        sess.outbox.confirm(ack)
        sess.consumed = max(sess.consumed, consumed)

    @staticmethod
    def _accept_input(sess: _Session, event_seq: int) -> None:
        sess.input_seq += 1
        sess.outbox.push(
            sw.input_accepted(
                session_id=sess.session_id,
                incarnation=sess.incarnation,
                event_seq=event_seq,
                input_seq=sess.input_seq,
                accepted_at_ms=sw.now_ms(),
            )
        )

    @staticmethod
    def _receive_prefill(sess: _Session, header: dict[str, Any]) -> None:
        work_id = _checked(header, "work_id", str)
        attempt_id = _checked(header, "attempt_id", str)
        text_list = _checked(header, "text_list", list)
        if len(text_list) != 1 or type(text_list[0]) is not str:
            raise AdapterError("text_list must contain exactly one backend prefill")
        text = text_list[0]
        existing = sess.prefill_attempts.get(attempt_id)
        if existing is not None:
            if existing[0] != text:
                raise AdapterError("attempt_id reused with different prefill text")
            sess.outbox.push({**existing[1], "deduplicated": True})
            return
        if sess.prefill_request is not None:
            raise AdapterError("a backend prefill is already waiting to be applied")
        sess.prefill_request = {"work_id": work_id, "attempt_id": attempt_id, "text": text}

    def _spawn_close(self, sess: _Session, reason: str) -> None:
        task = asyncio.create_task(self.close_session(sess, reason))
        self._closers.add(task)
        task.add_done_callback(self._closers.discard)

    async def close_session(self, sess: _Session, reason: str) -> None:
        async with self._open_lock:
            await self._close_locked(sess, reason)

    async def _close_locked(self, sess: _Session, reason: str) -> None:
        """Close ``sess``; the caller holds ``_open_lock`` so no open races the teardown."""
        if sess.closed:
            return
        sess.closed = True
        if sess.expiry is not None:
            sess.expiry.cancel()
            sess.expiry = None
        stop_fn = getattr(self._official, "set_session_stop", None)
        if callable(stop_fn):
            stop_fn()
        # Cancelling would not stop a step running in its thread; wait for the
        # driver to return before destroying the KV it is using.
        if sess.driver is not None:
            await asyncio.wait([sess.driver])
        await asyncio.to_thread(self._teardown_model)
        if sess.failure is None:
            sess.outbox.push(
                sw.session_closed(
                    session_id=sess.session_id,
                    incarnation=sess.incarnation,
                    closed_at_ms=sw.now_ms(),
                )
            )
        if sess.link is not None:
            sess.link.set()
        _LOG.info("close_session %s reason=%s", sess.session_id, reason)

    def _teardown_model(self) -> None:
        as_simplex = getattr(self._model, "as_simplex", None)
        if callable(as_simplex):
            self._model = as_simplex()  # releases KV / returns simplex model
            self._official = _official(self._model)
            self._needs_duplex = callable(getattr(self._model, "as_duplex", None))

    async def health(self) -> dict[str, Any]:
        sp = self._system_prompt
        return {
            "model_revision": self._model_revision,
            "delegate_audio_filtered": self.delegate_audio_filtered,
            "duplex_decoding": {
                "length_penalty": self._length_penalty,
                "implementation": self._decoding_implementation,
            },
            "uses_model_default_system_prompt": sp is None,
            "system_prompt_preview": (sp[:120] + "…") if sp else None,
        }

    async def stats(self) -> dict[str, Any]:
        return {
            "gpu_memory_after_load": self._memory_after_load,
            "gpu_memory": gpu_memory(),
            "steps": self._steps.summary(),
        }

    # -- driver -------------------------------------------------------------

    async def _driver_loop(self, sess: _Session) -> None:
        """The only caller of the model: prefill writes and steps run here, one at a time."""
        try:
            while not sess.closed:
                if (
                    sess.prefill_request is not None
                    and sess.fg_gen is None
                    and sess.resume_gen is None
                ):
                    await self._apply_prefill(sess)
                    continue
                if sess.emitted - sess.consumed < OUTPUT_WINDOW:
                    started = time.perf_counter()
                    frame = await self._step_once(sess)
                    if frame is not None:
                        self._steps.add((time.perf_counter() - started) * 1000)
                        sess.outbox.push(*frame)
                        sess.emitted += 1
                        continue
                if self._wakeup.is_set():
                    self._wakeup.clear()
                    continue
                await asyncio.sleep(0.02)
        except Exception as exc:  # noqa: BLE001 - never let the driver die silently
            _LOG.exception("driver loop crashed")
            # A step the stop request interrupted is the close itself, not a failure.
            if not sess.closed:
                sess.failure = f"{type(exc).__name__}: {exc}"
                # close_session waits for this task to end, so it cannot be awaited here.
                self._spawn_close(sess, "model_error")

    async def _apply_prefill(self, sess: _Session) -> None:
        request = sess.prefill_request
        gen_id, gen_epoch = self._allocate_generation(sess, prefix="backend")
        # Bypass the memory hijack: write <backend> directly to the live KV via
        # the official duplex, then generate resumes on the same KV.
        await asyncio.to_thread(
            self._official.streaming_prefill, None, None, [request["text"]], 1, False
        )
        sess.prefill_request = None
        sess.resume_gen = (gen_id, gen_epoch)
        sess.step_seq = 0
        sess.audio_seq = 0
        sess.prev_total_len = len(self._official.total_ids)
        applied = sw.prefill_applied(
            session_id=sess.session_id,
            incarnation=sess.incarnation,
            work_id=request["work_id"],
            attempt_id=request["attempt_id"],
            generation_id=gen_id,
            generation_epoch=gen_epoch,
            kv_position=sess.prev_total_len,
            applied_at_ms=sw.now_ms(),
            deduplicated=False,
        )
        sess.prefill_attempts[request["attempt_id"]] = (request["text"], applied)
        sess.outbox.push(dict(applied))
        _LOG.info(
            "prefill applied work=%s attempt=%s gen=%s epoch=%d",
            request["work_id"],
            request["attempt_id"],
            gen_id,
            gen_epoch,
        )

    async def _step_once(self, sess: _Session) -> tuple[dict[str, Any], bytes] | None:
        """One model step as an ``output`` frame, or None while a bucket is still filling."""
        if sess.resume_gen is not None:
            gen_id, gen_epoch = sess.resume_gen
            if sess.step_seq:
                bucket = self._collect_bucket(sess)
                if bucket is None:
                    import numpy as np

                    bucket = _Bucket(
                        audio=np.zeros(self._chunk_samples, dtype=np.float32), frames=[]
                    )
                await asyncio.to_thread(
                    self._official.streaming_prefill,
                    bucket.audio,
                    bucket.frames,
                    None,
                    1,
                    False,
                )
            step, pcm, finished = await self._generate_step(
                sess, gen_id, gen_epoch, is_backend=True
            )
            if finished:
                sess.resume_gen = None
            return step, pcm
        bucket = self._collect_bucket(sess)
        if bucket is None:
            return None
        await asyncio.to_thread(
            self._model.streaming_prefill,
            bucket.audio,
            bucket.frames,
            None,
            1,
            False,
        )
        if sess.fg_gen is None:
            sess.fg_gen = self._allocate_generation(sess, prefix="foreground")
            sess.step_seq = 0
            sess.audio_seq = 0
            sess.prev_total_len = len(self._official.total_ids)
        gen_id, gen_epoch = sess.fg_gen
        step, pcm, finished = await self._generate_step(
            sess, gen_id, gen_epoch, is_backend=False
        )
        if finished:
            sess.fg_gen = None
        return step, pcm

    async def _generate_step(
        self, sess: _Session, gen_id: str, gen_epoch: int, *, is_backend: bool
    ) -> tuple[dict[str, Any], bytes, bool]:
        gen_fn = (self._official if is_backend else self._model).streaming_generate
        # delta tracking: snapshot total_ids length right before generate
        prev_len = sess.prev_total_len
        result = await asyncio.to_thread(
            lambda: gen_fn(
                prompt_wav_path=None,
                decode_mode="sampling",
                max_new_speak_tokens_per_chunk=self._max_speak,
                **self._generation_options,
            )
        )
        ids = list(self._official.total_ids)
        delta = tuple(ids[prev_len:])
        if any(
            self._special[token] in delta for token in ("<delegate>", "</delegate>")
        ):
            _LOG.info(
                "model emitted delegate control tokens session=%s generation=%s ids=%s",
                sess.session_id,
                gen_id,
                delta,
            )
        if not result.get("is_listen"):
            _LOG.info(
                "model speak session=%s backend=%s end=%s ids=%s",
                sess.session_id,
                is_backend,
                result.get("end_of_turn"),
                delta,
            )
        sess.prev_total_len = len(ids)
        wave = None if result.get("is_listen") else result.get("audio_waveform")
        pcm = sw.float32_to_pcm16le(wave)
        audio_chunk_seq = None
        if pcm:
            sess.audio_seq += 1
            audio_chunk_seq = sess.audio_seq
        sess.step_seq += 1
        # Foreground listen units leave an idle boundary for backend prefill.
        # After prefill, listen is only a pause before the spoken answer; its
        # silence must not complete delivery or detach the answer from its Work.
        finished = bool(
            result.get("end_of_turn") or (result.get("is_listen") and not is_backend)
        )
        finish_reason = "turn_eos" if finished else None
        step = sw.model_output_step(
            session_id=sess.session_id,
            incarnation=sess.incarnation,
            generation_id=gen_id,
            generation_epoch=gen_epoch,
            step_seq=sess.step_seq,
            input_seq_cutoff=sess.consumed_input_seq,
            at_ms=sw.now_ms(),
            total_token_ids=delta,
            audio=sw.audio_header() if pcm else None,
            audio_chunk_seq=audio_chunk_seq,
            turn_finished=finished,
            finish_reason=finish_reason,
        )
        return step, pcm, finished

    def _collect_bucket(self, sess: _Session) -> Any | None:
        if sess.event_seq == 0:
            return None
        # bucket is pure-python; nothing here needs to run in an await context
        if sess.audio_samples < self._chunk_samples and not sess.frames:
            return None
        if sess.audio_samples < self._chunk_samples:
            # frame(s) but no enough audio yet -> wait for audio (v1 needs audio to drive)
            return None
        # consume up to one chunk of audio + any pending frames
        take_samples = min(sess.audio_samples, self._chunk_samples)
        take_bytes = take_samples * 2
        pcm = bytes(sess.audio_buf[:take_bytes])
        del sess.audio_buf[:take_bytes]
        sess.audio_samples -= take_samples
        import numpy as np

        audio = sw.pcm16le_to_float32(pcm) if pcm else np.zeros(0, dtype=np.float32)
        # decode wire image bytes -> PIL RGB for the real model; invalid image
        # bytes fall back raw (see serving_wire.decode_frames).
        frames = sw.decode_frames(list(sess.frames))
        sess.frames.clear()
        sess.consumed_input_seq = sess.input_seq
        return _Bucket(audio=audio, frames=frames)

    def _allocate_generation(self, sess: _Session, *, prefix: str) -> tuple[str, int]:
        sess.gen_epoch += 1
        gen_id = f"{prefix}-{sess.session_id}:{sess.gen_epoch}"
        return gen_id, sess.gen_epoch

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _fence_event(sess: _Session, event_seq: int) -> None:
        expected = sess.event_seq + 1
        if event_seq != expected:
            raise EventSeqMismatch(
                f"event_seq must be contiguous; expected {expected}, got {event_seq}"
            )
        sess.event_seq = event_seq


class _Bucket:
    __slots__ = ("audio", "frames")

    def __init__(self, audio: Any, frames: list[bytes]) -> None:
        self.audio = audio
        self.frames = frames


# ---------------------------------------------------------------------------
# Real model loading (heavy, runs in a worker thread)
# ---------------------------------------------------------------------------


def _load_real_blocking(
    model_path: str, memory_minutes: int, ref_audio_path: str | None, model_type: str = "omni"
):
    import librosa
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer

    if model_type not in {"omni", "audio"}:
        raise ValueError("Unknown model type")
    model = AutoModel.from_pretrained(
        model_path,
        trust_remote_code=True,
        local_files_only=True,
        attn_implementation="sdpa",
        torch_dtype=torch.bfloat16,
        **({"init_vision": False, "init_audio": True, "init_tts": True} if model_type == "audio" else {}),
    )
    model.eval().cuda()
    if model_type == "omni":
        model.use_memory(memory_minutes=memory_minutes)
    model = model.as_duplex(generate_audio=True) if model_type == "audio" else model.as_duplex()
    tokenizer = getattr(model, "tokenizer", None)
    if tokenizer is None:
        tokenizer = AutoTokenizer.from_pretrained(
            model_path, trust_remote_code=True, local_files_only=True
        )
    ref_audio = None
    if ref_audio_path:
        ref_audio, _ = librosa.load(ref_audio_path, sr=16_000, mono=True)
        ref_audio = ref_audio.astype(np.float32)
    return model, tokenizer, ref_audio
