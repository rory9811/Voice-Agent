#!/usr/bin/env python3
"""Run streaming full-duplex Realtime-Venus-Audio inference."""

import argparse
import sys
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
import torch
from transformers import AutoModel, set_seed


DEFAULT_SYSTEM_PROMPT = "Streaming Omni Conversation."
INPUT_SAMPLE_RATE = 16000
INPUT_TAIL_SECONDS = 10
TTS_SAMPLE_RATE = 24000


def load_model(model_dir: Path):
    set_seed(42)
    print(f"Loading model from {model_dir} ...")
    model = AutoModel.from_pretrained(
        str(model_dir),
        trust_remote_code=True,
        local_files_only=True,
        attn_implementation="sdpa",
        torch_dtype=torch.bfloat16,
        init_vision=False,
        init_audio=True,
        init_tts=True,
    )
    model.eval().cuda()
    print("Model loaded.")
    return model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True, help="Local model directory")
    parser.add_argument("--audio", required=True, help="Input audio or video file")
    parser.add_argument("--output", required=True, help="Generated speech WAV path")
    parser.add_argument(
        "--prompt", default="", help="Optional text injected with the final chunk"
    )
    parser.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT)
    parser.add_argument(
        "--decode-mode", choices=("sampling", "greedy"), default="sampling"
    )
    parser.add_argument("--max-new-tokens-per-chunk", type=int, default=20)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--top-k", type=int, default=20)
    parser.add_argument("--top-p", type=float, default=0.8)
    parser.add_argument("--listen-prob-scale", type=float, default=1.0)
    return parser.parse_args()


def audio_samples(waveform) -> np.ndarray:
    samples = np.asarray(waveform, dtype=np.float32).squeeze()
    if samples.ndim == 1:
        return samples[:, None]
    if samples.ndim != 2:
        raise ValueError(f"Unexpected generated audio shape: {samples.shape}")
    return samples.T if samples.shape[0] <= 8 else samples


def save_audio(timed_audio, output_path: Path) -> None:
    chunks = [
        (round(start_time * TTS_SAMPLE_RATE), audio_samples(waveform))
        for start_time, waveform in timed_audio
    ]
    channels = max((samples.shape[1] for _, samples in chunks), default=1)
    end_sample = max((start + len(samples) for start, samples in chunks), default=1)
    output = np.zeros((end_sample, channels), dtype=np.float32)

    for start, samples in chunks:
        if samples.shape[1] == 1 and channels > 1:
            samples = np.repeat(samples, channels, axis=1)
        elif samples.shape[1] != channels:
            raise ValueError("Generated audio chunks have inconsistent channel counts")
        output[start : start + len(samples)] += samples

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(output_path, np.clip(output, -1.0, 1.0), TTS_SAMPLE_RATE)


def stream_audio(audio: np.ndarray, duplex, args: argparse.Namespace):
    chunk_samples = round(duplex.CHUNK_MS * duplex.SAMPLE_RATE / 1000)
    total_chunks = max(1, (len(audio) + chunk_samples - 1) // chunk_samples)
    timed_audio = []
    emitted_text = False

    for index in range(total_chunks):
        chunk = audio[index * chunk_samples : (index + 1) * chunk_samples]
        chunk = np.pad(chunk, (0, chunk_samples - len(chunk)))
        is_last = index == total_chunks - 1

        prefill = duplex.streaming_prefill(
            audio_waveform=chunk,
            text_list=[args.prompt.strip()] if is_last and args.prompt.strip() else None,
        )
        if not prefill.get("success"):
            reason = prefill.get("reason") or "unknown prefill error"
            raise RuntimeError(f"Audio chunk {index + 1} failed: {reason}")

        result = duplex.streaming_generate(
            max_new_speak_tokens_per_chunk=args.max_new_tokens_per_chunk,
            decode_mode=args.decode_mode,
            temperature=args.temperature,
            top_k=args.top_k,
            top_p=args.top_p,
            listen_prob_scale=args.listen_prob_scale,
        )
        text = result.get("text", "")
        if text:
            print(text, end="", flush=True)
            emitted_text = True

        if result.get("audio_waveform") is not None and not result.get("is_listen"):
            start_time = index * duplex.CHUNK_MS / 1000
            timed_audio.append((start_time, result["audio_waveform"]))

        state = "listen" if result.get("is_listen") else "speak"
        print(f"[{index + 1}/{total_chunks}] {state}", file=sys.stderr, flush=True)

    if emitted_text:
        print(flush=True)
    return timed_audio


def main() -> None:
    args = parse_args()
    model_dir = Path(args.model_path).expanduser().resolve()
    audio_path = Path(args.audio).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    audio, _ = librosa.load(audio_path, sr=INPUT_SAMPLE_RATE, mono=True)
    if not len(audio):
        raise ValueError(f"Audio file is empty: {audio_path}")
    audio = np.pad(audio.astype(np.float32), (0, INPUT_TAIL_SECONDS * INPUT_SAMPLE_RATE))

    duplex = load_model(model_dir).as_duplex(generate_audio=True)
    duplex.prepare(prefix_system_prompt=args.system_prompt)
    print(
        f"Streaming {len(audio) / duplex.SAMPLE_RATE:.2f}s from {audio_path} "
        f"(appended {INPUT_TAIL_SECONDS}s silence)",
        file=sys.stderr,
        flush=True,
    )
    with torch.inference_mode():
        timed_audio = stream_audio(audio, duplex, args)

    save_audio(timed_audio, output_path)
    print(f"Saved generated audio to {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
