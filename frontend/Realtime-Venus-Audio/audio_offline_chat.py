#!/usr/bin/env python3
"""Run deterministic, turn-based Realtime-Venus-Audio inference."""

import argparse
import os
import sys
from pathlib import Path

import librosa
import torch
from transformers import AutoModel, AutoTokenizer, set_seed


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."


def resolve_model_dir(configured_path: str | None = None) -> Path:
    configured_path = configured_path or os.environ.get("REALTIME_VENUS_MODEL_PATH")
    if configured_path:
        return Path(configured_path).expanduser().resolve()

    local_model = PROJECT_DIR / "Realtime-Venus-Audio"
    if (local_model / "config.json").is_file():
        return local_model

    # Allow this standalone script to use the repository's shared downloader.
    if str(PROJECT_DIR) not in sys.path:
        sys.path.insert(0, str(PROJECT_DIR))
    from download_models import download_models

    return download_models(model="audio", local_dir=PROJECT_DIR)["audio"]


def load_model(model_dir: Path):
    set_seed(42)
    print(f"Loading model from {model_dir} ...")
    tokenizer = AutoTokenizer.from_pretrained(
        str(model_dir),
        trust_remote_code=True,
        local_files_only=True,
        fix_mistral_regex=True,
    )
    model = AutoModel.from_pretrained(
        str(model_dir),
        trust_remote_code=True,
        local_files_only=True,
        attn_implementation="sdpa",
        torch_dtype=torch.bfloat16,
        init_vision=False,
        init_audio=True,
        init_tts=False,
    )
    model.eval().cuda()
    print("Model loaded.")
    return model, tokenizer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", help="Local model directory")
    parser.add_argument(
        "--audio", help="Audio or video file containing the input speech"
    )
    parser.add_argument(
        "--prompt", default="", help="Optional text appended after the audio"
    )
    parser.add_argument("--system-prompt", default=DEFAULT_SYSTEM_PROMPT)
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--num-beams", type=int, default=1)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    model_dir = resolve_model_dir(args.model_path)
    audio_path = (
        Path(args.audio).expanduser().resolve()
        if args.audio
        else model_dir / "assets" / "speech_in.mp4"
    )
    audio, _ = librosa.load(audio_path, sr=16000, mono=True)

    content = [audio]
    if args.prompt.strip():
        content.append(args.prompt.strip())
    messages = []
    if args.system_prompt.strip():
        messages.append({"role": "system", "content": args.system_prompt.strip()})
    messages.append({"role": "user", "content": content})

    model, tokenizer = load_model(model_dir)
    print(f"Running model.chat on {audio_path} ...")
    with torch.inference_mode():
        response = model.chat(
            msgs=messages,
            tokenizer=tokenizer,
            do_sample=False,
            num_beams=args.num_beams,
            max_new_tokens=args.max_new_tokens,
            enable_thinking=False,
            use_tts_template=True,
            generate_audio=False,
        )
    print("" if response is None else str(response).strip())


if __name__ == "__main__":
    main()
