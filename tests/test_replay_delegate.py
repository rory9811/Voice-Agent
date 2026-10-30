import importlib.util
import math
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch

_PATH = Path(__file__).parents[1] / "deploy/model/replay/replay_delegate.py"
_SPEC = importlib.util.spec_from_file_location("replay_delegate", _PATH)
replay_delegate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(replay_delegate)

DELEGATE, LISTEN, SPEAK, CHUNK_EOS, UNIT_END = 3, 1, 2, 4, 6
TOKEN_IDS = {
    "<delegate>": DELEGATE, "<|listen|>": LISTEN, "<|speak|>": SPEAK,
    "<|chunk_eos|>": CHUNK_EOS, "<|turn_eos|>": 5, "</unit>": UNIT_END, "</delegate>": 7,
}


class FakeDuplex:
    CHUNK_MS = 1000
    SAMPLE_RATE = 4

    def __init__(self, tokens_per_step):
        self.tokenizer = SimpleNamespace(convert_tokens_to_ids=TOKEN_IDS.__getitem__)
        self.decoder = SimpleNamespace(decode=lambda logits, **kwargs: self._next())
        self.total_ids = [99]  # left over from the system prompt
        self.chunks = []
        self._tokens = iter(tokens_per_step)

    def _next(self):
        return next(self._tokens)

    def streaming_prefill(self, audio_waveform):
        self.chunks.append(len(audio_waveform))
        return {"success": True}

    def streaming_generate(self, temperature, **_):
        logits = torch.tensor([[0.0, 0.0, 0.0, math.log(9.0) * temperature]])
        token = self.decoder.decode(logits=logits, temperature=temperature)
        self.total_ids.append(token)
        return {"is_listen": token == LISTEN}


class ReplayTests(unittest.TestCase):
    def test_counts_a_delegation_and_its_peak_probability(self):
        duplex = FakeDuplex([SPEAK, DELEGATE] + [LISTEN] * 20)
        real_decode = duplex.decoder.decode
        delegated, first_speak, peak, ids = replay_delegate.replay(duplex, np.zeros(6, dtype=np.float32))
        self.assertTrue(delegated)
        self.assertEqual(first_speak, 1)
        self.assertAlmostEqual(peak, 0.75, places=5)  # 9 against three tokens at 1
        self.assertNotIn(99, ids)
        self.assertIs(duplex.decoder.decode, real_decode)
        self.assertEqual(duplex.chunks, [4] * 12)  # 1.5 s padded, then 10 s of silence

    def test_a_session_of_only_listening_counts_nothing(self):
        duplex = FakeDuplex([LISTEN] * 20)
        delegated, first_speak, _, _ = replay_delegate.replay(duplex, np.zeros(4, dtype=np.float32))
        self.assertFalse(delegated)
        self.assertIsNone(first_speak)

    def test_first_speak_is_the_seconds_heard_when_it_first_spoke(self):
        duplex = FakeDuplex([LISTEN, LISTEN, SPEAK] + [LISTEN] * 20)
        _, first_speak, _, _ = replay_delegate.replay(duplex, np.zeros(4, dtype=np.float32))
        self.assertEqual(first_speak, 3)

    def test_longest_unit_counts_only_text(self):
        tokenizer = SimpleNamespace(convert_tokens_to_ids=TOKEN_IDS.__getitem__)
        ids = [SPEAK, 10, 11, 12, CHUNK_EOS, UNIT_END, SPEAK, 10, CHUNK_EOS, UNIT_END]
        self.assertEqual(replay_delegate.longest_unit(ids, tokenizer), 3)

    def test_longest_unit_skips_muted_task_text(self):
        tokenizer = SimpleNamespace(convert_tokens_to_ids=TOKEN_IDS.__getitem__)
        ids = [SPEAK, 10, DELEGATE, 11, 12, 13, CHUNK_EOS, UNIT_END, SPEAK, 14, 7, 15, 16, CHUNK_EOS, UNIT_END]
        self.assertEqual(replay_delegate.longest_unit(ids, tokenizer), 2)


if __name__ == "__main__":
    unittest.main()
