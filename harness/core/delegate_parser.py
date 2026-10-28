"""Incremental private delegate spans with visible speech before and after."""

from __future__ import annotations

from .models import ParserStep


class DelegateParser:
    """Split visible TTS text from a streamed delegate instruction.

    One delegate request is admitted per model turn. Normal speech may resume
    after its closing tag. A second span or malformed request is rejected;
    partial tags and private text remain hidden across transport chunks.
    """

    OPEN_TAG = "<delegate>"
    CLOSE_TAG = "</delegate>"

    def __init__(self, max_query_chars: int = 4_000) -> None:
        self._max_query_chars = max_query_chars
        self._mode = "visible"
        self._visible_pending = ""
        self._query_buffer = ""
        self._closed = False
        self._delegate_seen = False

    @staticmethod
    def _longest_open_prefix(value: str) -> int:
        upper = min(len(value), len(DelegateParser.OPEN_TAG) - 1)
        for length in range(upper, 0, -1):
            if value.endswith(DelegateParser.OPEN_TAG[:length]):
                return length
        return 0

    def feed(self, delta: str) -> ParserStep:
        """Consume one text delta without leaking a partial control tag to TTS."""

        if not delta:
            return ParserStep()
        if self._closed:
            return ParserStep()

        if self._mode == "visible":
            return self._feed_visible(delta)
        return self._feed_delegate(delta)

    def _feed_visible(self, delta: str) -> ParserStep:
        had_candidate = bool(self._visible_pending)
        self._visible_pending += delta
        position = self._visible_pending.find(self.OPEN_TAG)
        if position < 0:
            held = self._longest_open_prefix(self._visible_pending)
            safe_end = len(self._visible_pending) - held
            visible = self._visible_pending[:safe_end]
            self._visible_pending = self._visible_pending[safe_end:]
            return ParserStep(
                visible_text=visible,
                # A malformed old prefix may be flushed while a new ``<`` at
                # the end of this same delta begins another candidate.
                delegate_candidate_started=not self._delegate_seen
                and held > 0
                and (not had_candidate or bool(visible)),
                delegate_candidate_abandoned=not self._delegate_seen
                and had_candidate
                and bool(visible),
            )

        visible = self._visible_pending[:position]
        if self._delegate_seen:
            self._closed = True
            self._visible_pending = ""
            return ParserStep(
                visible_text=visible,
                protocol_error="only one delegate request is allowed per model turn",
            )
        remainder = self._visible_pending[position + len(self.OPEN_TAG) :]
        self._visible_pending = ""
        self._mode = "delegate"
        step = self._feed_delegate(remainder)
        return ParserStep(
            visible_text=visible + step.visible_text,
            delegate_candidate_started=not had_candidate or position > 0,
            delegate_candidate_abandoned=had_candidate and position > 0,
            opened_delegate=True,
            delegate_query=step.delegate_query,
            protocol_error=step.protocol_error,
        )

    def _feed_delegate(self, delta: str) -> ParserStep:
        self._query_buffer += delta
        position = self._query_buffer.find(self.CLOSE_TAG)
        # Normal speech after a closed span does not count toward its limit.
        size = (
            position
            if position >= 0
            else len(self._query_buffer) - self._closing_prefix_length()
        )
        if size > self._max_query_chars:
            self._closed = True
            return ParserStep(
                protocol_error="delegate query exceeds configured character limit"
            )

        if position < 0:
            return ParserStep()

        query = self._query_buffer[:position].strip()
        trailing = self._query_buffer[position + len(self.CLOSE_TAG) :]
        self._query_buffer = ""
        if not query:
            self._closed = True
            return ParserStep(protocol_error="delegate query must not be empty")
        self._delegate_seen = True
        self._mode = "visible"
        tail = self._feed_visible(trailing)
        return ParserStep(
            visible_text=tail.visible_text,
            delegate_query=query,
            protocol_error=tail.protocol_error,
        )

    def _closing_prefix_length(self) -> int:
        for size in range(min(len(self._query_buffer), len(self.CLOSE_TAG) - 1), 0, -1):
            if self._query_buffer.endswith(self.CLOSE_TAG[:size]):
                return size
        return 0

    def finish_turn(self) -> ParserStep:
        """Flush a normal turn or flag an unterminated delegate block."""

        if self._closed:
            self.reset()
            return ParserStep()
        if self._mode == "delegate":
            self.reset()
            return ParserStep(protocol_error="model turn ended before </delegate>")
        visible = self._visible_pending
        had_candidate = bool(self._visible_pending)
        self.reset()
        return ParserStep(
            visible_text=visible,
            delegate_candidate_abandoned=had_candidate,
        )

    def reset(self) -> None:
        self._mode = "visible"
        self._visible_pending = ""
        self._query_buffer = ""
        self._closed = False
        self._delegate_seen = False
