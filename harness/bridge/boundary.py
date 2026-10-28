"""Realtime-Venus-Omni boundary: raw cumulative token IDs, private delegate capture and backend prefill."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from harness.core.frontend import assemble_frontend_message
from harness.core.venus import VenusOmniDelegateBridge


@dataclass(frozen=True)
class VenusOmniTokenStep:
    """The newly observed, frontend-safe output from one cumulative poll."""

    visible_text: str = ""
    delegate_queries: tuple[str, ...] = ()
    saw_delegate: bool = False
    consumed_token_count: int = 0
    cursor: int = 0
    stream_reset: bool = False


@dataclass(frozen=True)
class BackendPrefill:
    """One deduplicated private payload ready for ``streaming_prefill``."""

    work_id: str
    text: str
    feedback_id: str = ""


class VenusOmniDuplexBoundary:
    """Adapt VenusOmni ``total_ids`` and Harness results at the model boundary.

    ``duplex_model.total_ids`` is cumulative within a generation.  Call
    :meth:`consume_total_ids` as often as desired; only its append-only suffix
    is forwarded to :class:`harness.core.VenusOmniDelegateBridge`.

    A shorter or non-prefix token buffer indicates that the producer reset its
    generation state.  The previous Harness turn is finished before accepting
    the new stream.  A non-empty old prefix is ambiguous: it may contain an
    opening private control token whose parser state has just been reset.  Such
    a stream is quarantined until the producer exposes an empty cumulative
    buffer, so private body tokens can never be replayed as visible speech.
    """

    def __init__(
        self,
        *,
        harness: Any,
        session_id: str,
        tokenizer: Any,
        special_token_ids: Mapping[str, int] | None = None,
    ) -> None:
        if not session_id:
            raise ValueError("session_id is required")
        self._harness = harness
        self._session_id = session_id
        self._bridge = VenusOmniDelegateBridge(
            harness=harness,
            session_id=session_id,
            tokenizer=tokenizer,
            special_token_ids=special_token_ids,
        )
        self._last_total_ids: tuple[int, ...] = ()
        self._injected_work_ids: set[str] = set()
        self._token_lock = asyncio.Lock()
        self._quarantined = False

    @property
    def consumed_cursor(self) -> int:
        """Number of token IDs consumed from the latest cumulative buffer."""

        return len(self._last_total_ids)

    async def consume_total_ids(
        self,
        total_ids: Any,
        *,
        at_ms: int,
        cutoff_sequence: int | None = None,
    ) -> VenusOmniTokenStep:
        """Consume only new IDs from VenusOmni's cumulative ``total_ids``.

        Tensor-like objects with ``tolist()`` and a single batch dimension are
        accepted without importing torch or numpy.
        """

        async with self._token_lock:
            return await self._consume_total_ids_locked(
                total_ids,
                at_ms=at_ms,
                cutoff_sequence=cutoff_sequence,
            )

    async def _consume_total_ids_locked(
        self,
        total_ids: Any,
        *,
        at_ms: int,
        cutoff_sequence: int | None,
    ) -> VenusOmniTokenStep:
        current = _normalise_total_ids(total_ids)

        if self._quarantined:
            self._last_total_ids = current
            if not current:
                # The producer has now demonstrated a real stream boundary.  The
                # empty observation itself remains suppressed; the next non-empty
                # buffer starts a clean generation from cursor zero.
                self._quarantined = False
            return VenusOmniTokenStep(
                saw_delegate=True,
                consumed_token_count=0,
                cursor=len(current),
                stream_reset=True,
            )

        previous = self._last_total_ids
        append_only = (
            len(current) >= len(previous) and current[: len(previous)] == previous
        )

        reset = not append_only
        reset_visible = ""
        if reset:
            # Reset both the raw-token bridge and the Harness text parser.  This
            # rejects an unfinished delegate instead of leaking it into a new
            # model turn.
            reset_visible = await self._bridge.finish_turn(at_ms=at_ms)

            is_old_prefix = (
                len(current) < len(previous) and current == previous[: len(current)]
            )
            if is_old_prefix and current:
                # Never adopt a non-empty old prefix as a clean parser baseline.
                # It may contain <delegate> even though finish_turn just cleared
                # the bridge's private-state bit.  Ignore everything until an
                # actual empty buffer proves that the producer reset total_ids.
                self._last_total_ids = current
                self._quarantined = True
                return VenusOmniTokenStep(
                    saw_delegate=True,
                    consumed_token_count=0,
                    cursor=len(current),
                    stream_reset=True,
                )
            new_ids: tuple[int, ...] = () if is_old_prefix else current
        else:
            new_ids = current[len(previous) :]

        # Advance before crossing the async Harness boundary.  If a downstream
        # consumer fails after accepting a closed delegate, retrying this poll
        # must not submit that delegate a second time.
        self._last_total_ids = current

        visible = ""
        queries: tuple[str, ...] = ()
        saw_delegate = False
        if new_ids:
            visible, queries, saw_delegate = await self._bridge.feed(
                new_ids,
                at_ms=at_ms,
                cutoff_sequence=cutoff_sequence,
            )

        return VenusOmniTokenStep(
            visible_text=reset_visible + visible,
            delegate_queries=queries,
            saw_delegate=saw_delegate,
            consumed_token_count=len(new_ids),
            cursor=len(current),
            stream_reset=reset,
        )

    async def finish_turn(
        self,
        *,
        at_ms: int,
        next_total_ids: Any = (),
    ) -> str:
        """Finish the Harness turn and reset the cumulative-token cursor.

        ``next_total_ids`` is an optional already-observed baseline.  Passing it
        is useful when the model owns and clears ``total_ids`` asynchronously;
        those IDs will not be replayed on the next poll.
        """

        async with self._token_lock:
            return await self._finish_turn_locked(
                at_ms=at_ms,
                next_total_ids=next_total_ids,
            )

    async def _finish_turn_locked(
        self,
        *,
        at_ms: int,
        next_total_ids: Any,
    ) -> str:
        visible = await self._bridge.finish_turn(at_ms=at_ms)
        baseline = _normalise_total_ids(next_total_ids)
        self._last_total_ids = baseline
        if self._quarantined:
            self._quarantined = bool(baseline)
            if self._quarantined:
                return ""
        return visible

    async def reset_turn(
        self,
        *,
        at_ms: int,
        current_total_ids: Any = (),
    ) -> str:
        """Abort/reset the current model turn without replaying a baseline."""

        async with self._token_lock:
            return await self._finish_turn_locked(
                at_ms=at_ms,
                next_total_ids=current_total_ids,
            )

    def prepare_backend_prefill(
        self,
        message: Mapping[str, Any],
    ) -> BackendPrefill | None:
        """Validate, deduplicate, and wrap one Harness frontend message.

        The returned text contains exactly one outer ``<backend>`` block.  A
        repeated ``feedback_id`` (or legacy work_id) returns ``None`` so
        polling/retries cannot inject the same private feedback twice.
        """

        if not isinstance(message, Mapping):
            raise TypeError("frontend message must be a mapping")
        if message.get("type") != "delegate_result":
            raise ValueError("frontend message type must be 'delegate_result'")

        work_id = message.get("work_id")
        if not isinstance(work_id, str) or not work_id.strip():
            raise ValueError("frontend message work_id must be non-blank text")
        work_id = work_id.strip()

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("frontend message text must be non-blank text")

        feedback_id = message.get("feedback_id", work_id)
        if not isinstance(feedback_id, str) or not feedback_id.strip():
            raise ValueError("feedback_id must be non-blank text")
        if feedback_id in self._injected_work_ids:
            return None

        payload = self._bridge.backend_text(text)
        self._injected_work_ids.add(feedback_id)
        return BackendPrefill(work_id=work_id, text=payload, feedback_id=feedback_id)

    def rollback_backend_prefill_reservation(self, work_id: str) -> None:
        """Undo a reservation only when the atomic delivery transition failed."""

        self._injected_work_ids.discard(work_id)

    async def next_backend_prefill(
        self,
        *,
        timeout_s: float | None = None,
    ) -> BackendPrefill | None:
        """Consume one terminal Harness result and prepare its private payload.

        Progress notices share their request ID with the later terminal result,
        so they must never enter the injection deduplication set.
        """

        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0.0:
                raise TimeoutError
            result = await self._harness.next_delegate_result(
                self._session_id,
                timeout_s=remaining,
            )
            if result.status == "pending":
                continue
            return self.prepare_backend_prefill(assemble_frontend_message(result))


def _normalise_total_ids(value: Any) -> tuple[int, ...]:
    tolist = getattr(value, "tolist", None)
    if callable(tolist):
        value = tolist()

    # VenusOmni integrations commonly expose shape [1, sequence_length].
    while (
        isinstance(value, (list, tuple))
        and len(value) == 1
        and isinstance(value[0], (list, tuple))
    ):
        value = value[0]

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError("total_ids must be a one-dimensional token sequence")

    normalised = []
    for raw_token_id in value:
        if isinstance(raw_token_id, (list, tuple)):
            raise TypeError("total_ids must have only one token dimension")
        try:
            token_id = int(raw_token_id)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("total_ids contains a non-integer token ID") from exc
        normalised.append(token_id)
    return tuple(normalised)


__all__ = [
    "BackendPrefill",
    "VenusOmniDuplexBoundary",
    "VenusOmniTokenStep",
]
