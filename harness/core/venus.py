"""Thin VenusOmni token adapter; no model loading or TTS implementation."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

VENUS_PROTOCOL_TOKENS = (
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


def venus_special_token_ids(tokenizer: Any) -> dict[str, int]:
    """Resolve the protocol tokens required by the verified Duplex API."""

    values = {
        token: tokenizer.convert_tokens_to_ids(token) for token in VENUS_PROTOCOL_TOKENS
    }
    invalid = [
        token for token, token_id in values.items() if token_id is None or token_id < 0
    ]
    if invalid:
        raise ValueError(
            "VenusOmni tokenizer is missing protocol tokens: {}".format(
                ", ".join(invalid)
            )
        )
    if len(set(values.values())) != len(values):
        raise ValueError("VenusOmni protocol tokens do not have unique token IDs")
    return values


class VenusOmniDelegateBridge:
    """Translate generated token IDs into private Harness delegate spans.

    The model's decoded ``result['text']`` may omit special tokens.  Reading
    ``duplex_model.total_ids`` is therefore required for private task capture.
    Native audio admission is handled separately using the serving capability.
    """

    def __init__(
        self,
        *,
        harness: Any,
        session_id: str,
        tokenizer: Any,
        special_token_ids: Optional[Mapping[str, int]] = None,
    ) -> None:
        token_ids = dict(special_token_ids or venus_special_token_ids(tokenizer))
        self._harness = harness
        self._session_id = session_id
        self._tokenizer = tokenizer
        self._open_id = token_ids["<delegate>"]
        self._close_id = token_ids["</delegate>"]
        self._turn_end_id = token_ids["<|turn_eos|>"]
        self._ignored_ids = {
            token_ids[name]
            for name in VENUS_PROTOCOL_TOKENS
            if name not in {"<delegate>", "</delegate>"}
        }
        self._chunk_boundary_ids = {
            token_ids[name]
            for name in ("<|speak|>", "<|chunk_eos|>", "<unit>", "</unit>")
        }
        self._inside_delegate = False
        self._invalid_delegate = False
        self._delegate_body_ids: list[int] = []

    async def feed(
        self,
        token_ids: Sequence[int],
        *,
        at_ms: int,
        cutoff_sequence: Optional[int] = None,
    ) -> tuple[str, tuple[str, ...], bool]:
        """Return TTS-safe text, newly closed queries, and control presence."""

        visible_parts: list[str] = []
        closed_queries: list[str] = []
        normal_ids: list[int] = []
        # Track private body chunks even when they contain no delimiters.
        # Filtered native audio can still contain safe speech from around the
        # span, while legacy models need whole-chunk muting.
        saw_delegate = self._inside_delegate

        async def flush_normal() -> None:
            text = self._decode(normal_ids)
            normal_ids.clear()
            if text:
                kwargs = {"at_ms": at_ms}
                if cutoff_sequence is not None:
                    kwargs["cutoff_sequence"] = cutoff_sequence
                visible = await self._harness.on_model_text(
                    self._session_id, text, **kwargs
                )
                if visible:
                    visible_parts.append(visible)

        for raw_token_id in token_ids:
            token_id = int(raw_token_id)
            if token_id == self._open_id:
                await flush_normal()
                saw_delegate = True
                if self._inside_delegate:
                    # A nested open token makes the whole private block
                    # malformed.  Swallow it and force an empty close through
                    # the parser later so no partial objective can be queued.
                    self._invalid_delegate = True
                    self._delegate_body_ids = []
                    continue
                self._inside_delegate = True
                self._invalid_delegate = False
                self._delegate_body_ids = []
                kwargs = {"at_ms": at_ms}
                if cutoff_sequence is not None:
                    kwargs["cutoff_sequence"] = cutoff_sequence
                visible = await self._harness.on_model_text(
                    self._session_id,
                    "<delegate>",
                    **kwargs,
                )
                if visible:
                    visible_parts.append(visible)
                continue
            # Deployed checkpoints may terminate a private task with native
            # turn_eos instead of </delegate>. Only a generated terminal token
            # closes it; transport EOF/finish_turn alone must still discard it.
            if (
                token_id in {self._close_id, self._turn_end_id}
                and self._inside_delegate
            ):
                saw_delegate = True
                query = (
                    ""
                    if self._invalid_delegate
                    else self._decode(self._delegate_body_ids).strip()
                )
                self._delegate_body_ids = []
                self._inside_delegate = False
                self._invalid_delegate = False
                kwargs = {"at_ms": at_ms}
                if cutoff_sequence is not None:
                    kwargs["cutoff_sequence"] = cutoff_sequence
                visible = await self._harness.on_model_text(
                    self._session_id,
                    query + "</delegate>",
                    **kwargs,
                )
                if visible:
                    visible_parts.append(visible)
                if query:
                    closed_queries.append(query)
                continue
            if token_id == self._close_id:
                # A stray closing control token is never user-visible text.
                saw_delegate = True
                continue
            if self._inside_delegate:
                if token_id in self._chunk_boundary_ids:
                    continue
                if token_id in self._ignored_ids:
                    self._invalid_delegate = True
                    self._delegate_body_ids = []
                elif not self._invalid_delegate:
                    self._delegate_body_ids.append(token_id)
            elif token_id not in self._ignored_ids:
                normal_ids.append(token_id)
        await flush_normal()
        return "".join(visible_parts), tuple(closed_queries), saw_delegate

    async def finish_turn(self, *, at_ms: int) -> str:
        self._inside_delegate = False
        self._invalid_delegate = False
        self._delegate_body_ids = []
        return await self._harness.finish_model_turn(self._session_id, at_ms=at_ms)

    @staticmethod
    def backend_text(text: str) -> str:
        """Build the verified VenusOmni ``streaming_prefill(text_list=...)`` input."""

        # Provider output is untrusted protocol input.  Remove every reserved
        # VenusOmni marker so a backend answer cannot open a second delegate,
        # terminate the current prefill, or switch the duplex model's mode.
        safe = text
        while True:
            cleaned = safe
            for token in VENUS_PROTOCOL_TOKENS:
                cleaned = cleaned.replace(token, "")
            if cleaned == safe:
                break
            safe = cleaned
        remaining = [token for token in VENUS_PROTOCOL_TOKENS if token in safe]
        if remaining:
            raise AssertionError(
                "backend protocol sanitization left reserved markers: {}".format(
                    ", ".join(remaining)
                )
            )
        safe = safe.strip()
        return "<backend>{}</backend>".format(safe)

    def _decode(self, token_ids: Sequence[int]) -> str:
        if not token_ids:
            return ""
        return self._tokenizer.decode(list(token_ids), skip_special_tokens=False)


__all__ = [
    "VENUS_PROTOCOL_TOKENS",
    "VenusOmniDelegateBridge",
    "venus_special_token_ids",
]
