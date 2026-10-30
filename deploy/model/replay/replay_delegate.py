#!/usr/bin/env python3
"""Replay phrases followed by a fixed spoken clip through the checkpoint's own duplex loop.

Counts delegations with the clip appended after every phrase. Bypasses the browser,
the microphone and the model server. The loop and sampling values are the model card's.
"""

import argparse
from pathlib import Path

CARD_GENERATION = dict(
    max_new_speak_tokens_per_chunk=20,
    decode_mode="sampling",
    temperature=0.7,
    top_k=20,
    top_p=0.8,
    listen_prob_scale=1.0,
)
TAIL_SECONDS = 10
# 0 is the best case, one breath; 1 is about the soonest a server could notice the user stopped.
GAPS_SECONDS = (0, 1)


def longest_unit(ids, tokenizer):
    """Most spoken text tokens in one one-second unit; about 5 is the trained pace.

    Task text inside <delegate> is muted, so it says nothing about speech pace.
    """
    convert = tokenizer.convert_tokens_to_ids
    control = {convert(token) for token in ("<|listen|>", "<|speak|>", "<|chunk_eos|>")}
    unit_end, turn_end = convert("</unit>"), convert("<|turn_eos|>")
    task_start, task_end = convert("<delegate>"), convert("</delegate>")
    longest = count = 0
    in_task = False
    for token in ids:
        if token == unit_end:
            count = 0
        elif token == task_start:
            in_task = True
        elif token in (task_end, turn_end):
            in_task = False
        elif token not in control and not in_task:
            count += 1
            longest = max(longest, count)
    return longest


def replay(duplex, audio):
    """Stream one prepared session.

    Returns (delegated, first_speak, peak_p_delegate, ids). first_speak is how many
    seconds of input the model had heard when it first spoke, or None if it never did.
    """
    import numpy as np
    import torch

    delegate_id = duplex.tokenizer.convert_tokens_to_ids("<delegate>")
    decoder = duplex.decoder
    inner = decoder.decode
    peak = 0.0

    def decode(logits, **kwargs):
        nonlocal peak
        probs = torch.softmax(logits[0].float() / kwargs["temperature"], dim=-1)
        peak = max(peak, probs[delegate_id].item())
        return inner(logits=logits, **kwargs)

    decoder.decode = decode
    start = len(duplex.total_ids)
    chunk_samples = round(duplex.CHUNK_MS * duplex.SAMPLE_RATE / 1000)
    audio = np.concatenate([audio, np.zeros(TAIL_SECONDS * duplex.SAMPLE_RATE, dtype=np.float32)])
    first_speak = None
    try:
        for second, index in enumerate(range(0, len(audio), chunk_samples), start=1):
            chunk = audio[index : index + chunk_samples]
            chunk = np.pad(chunk, (0, chunk_samples - len(chunk)))
            prefill = duplex.streaming_prefill(audio_waveform=chunk)
            if not prefill.get("success"):
                raise RuntimeError(f"Prefill failed: {prefill.get('reason')}")
            result = duplex.streaming_generate(**CARD_GENERATION)
            if first_speak is None and not result["is_listen"]:
                first_speak = second
    finally:
        decoder.decode = inner
    ids = list(duplex.total_ids[start:])
    return delegate_id in ids, first_speak, peak, ids


def main():
    import librosa
    import numpy as np
    import torch
    from transformers import AutoModel, set_seed

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", default="/weights/Realtime-Venus-Audio")
    parser.add_argument("--wav-dir", default=str(Path(__file__).parent))
    parser.add_argument("--repeats", type=int, default=10)
    args = parser.parse_args()

    set_seed(42)
    model = AutoModel.from_pretrained(
        args.model_path,
        trust_remote_code=True,
        local_files_only=True,
        attn_implementation="sdpa",
        torch_dtype=torch.bfloat16,
        init_vision=False,
        init_audio=True,
        init_tts=True,
    )
    model.eval().cuda()
    duplex = model.as_duplex(generate_audio=True)

    def load(path):
        return librosa.load(path, sr=16000, mono=True)[0].astype(np.float32)

    reference_path = str(Path(args.model_path) / "assets/HT_ref_audio.wav")
    reference_audio = load(reference_path)
    clip = load(Path(args.wav_dir) / "clip/handoff.wav")
    phrases = sorted(Path(args.wav_dir).glob("*.wav"))
    if not phrases:
        raise SystemExit(f"No .wav files in {args.wav_dir}")

    summary = []
    with torch.inference_mode():
        for gap in GAPS_SECONDS:
            for path in phrases:
                phrase = load(path)
                audio = np.concatenate([phrase, np.zeros(gap * 16000, dtype=np.float32), clip])
                clip_at = len(phrase) / 16000 + gap
                delegated = spoke = early = longest = 0
                peak = 0.0
                for run in range(args.repeats):
                    # What demos/model/adapter.py passes.
                    duplex.prepare(
                        prefix_system_prompt="Streaming Omni Conversation.",
                        ref_audio=reference_audio,
                        prompt_wav_path=reference_path,
                    )
                    hit, first_speak, run_peak, ids = replay(duplex, audio)
                    delegated += hit
                    spoke += first_speak is not None
                    early += first_speak is not None and first_speak <= clip_at
                    peak = max(peak, run_peak)
                    unit = longest_unit(ids, duplex.tokenizer)
                    longest = max(longest, unit)
                    reply = duplex.tokenizer.decode(
                        [i for i in ids if i not in {duplex.listen_token_id, duplex.speak_token_id}]
                    ).replace("</unit>", "").replace("<|chunk_eos|>", "")
                    print(
                        f"run gap={gap} {path.stem} {run} delegate={hit} clip_at={clip_at:.1f} "
                        f"first_speak={first_speak} peak={run_peak:.4f} longest_unit={unit} {reply!r}",
                        flush=True,
                    )
                summary.append((gap, path.stem, delegated, spoke, early, peak, longest))

    print(f"\nSUMMARY repeats={args.repeats}")
    for gap, stem, delegated, spoke, early, peak, longest in summary:
        print(
            f"gap={gap} {stem:12} delegated={delegated} spoke={spoke} spoke_before_clip={early} "
            f"peak_p_delegate={peak:.4f} longest_unit={longest}"
        )


if __name__ == "__main__":
    main()
