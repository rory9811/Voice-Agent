import unittest
from unittest.mock import AsyncMock, MagicMock

from harness.bridge.contracts import ModelStep
from harness.bridge.host import VenusOmniServingHost
from harness.bridge.serving import (
    ModelOutputStep,
    RawTokenMode,
    SessionCapabilities,
    SessionOpened,
)
from harness.core.venus import VENUS_PROTOCOL_TOKENS


class HostClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_output_uses_local_clock(self):
        session = MagicMock(session_id="s")
        session.consume_total_ids = AsyncMock(return_value=ModelStep("hi", (), False, 1))
        session.finish_model_turn = AsyncMock(return_value="")
        opened = SessionOpened(
            session_id="s",
            incarnation=1,
            model="Realtime-Venus-Omni",
            model_revision="r",
            capabilities=SessionCapabilities(
                special_token_ids={t: i for i, t in enumerate(VENUS_PROTOCOL_TOKENS)},
                raw_token_mode=RawTokenMode.DELTA,
            ),
            opened_at_ms=0,
        )
        server_clock_ms = 10**12
        output = ModelOutputStep(
            session_id="s", incarnation=1, generation_id="foreground-s:1",
            generation_epoch=1, step_seq=1, input_seq_cutoff=0,
            at_ms=server_clock_ms, total_token_ids=(100,), audio=None,
            audio_chunk_seq=None, turn_finished=True, finish_reason="turn_eos",
        )
        host = VenusOmniServingHost(
            session=session, serving=MagicMock(next_event=AsyncMock(side_effect=[output])),
            playback=MagicMock(), opened=opened, clock=lambda: 5_000,
        )
        await host.next_output()
        self.assertEqual(session.consume_total_ids.await_args.kwargs["at_ms"], 5_000)
        self.assertEqual(session.finish_model_turn.await_args.kwargs["at_ms"], 5_000)


if __name__ == "__main__":
    unittest.main()
