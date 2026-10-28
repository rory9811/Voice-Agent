"""Run full-duplex inference with a spoken question embedded in the video."""

import os
from pathlib import Path

os.environ["MAX_NUM_FRAMES"] = "100000"

import torch
from minicpmo.utils import generate_duplex_video, get_video_frame_audio_segments
from transformers import AutoModel, set_seed


COOKBOOK_DIR = Path(__file__).resolve().parent
PROJECT_DIR = COOKBOOK_DIR.parent.parent
OUTPUT_DIR = COOKBOOK_DIR / "output"


def resolve_model_dir() -> Path:
    configured = os.environ.get("REALTIME_VENUS_MODEL_PATH")
    if configured:
        return Path(configured).expanduser().resolve()

    local_model = PROJECT_DIR / "Realtime-Venus-Omni"
    if not (local_model / "config.json").is_file():
        raise FileNotFoundError(
            f"Model checkpoint not found at {local_model}. Download "
            "inclusionAI/Realtime-Venus into the repository root first."
        )
    return local_model


def load_model(model_dir: Path):
    set_seed(42)
    print(f"Loading model from {model_dir} ...")
    model = AutoModel.from_pretrained(
        str(model_dir),
        trust_remote_code=True,
        local_files_only=True,
        attn_implementation="sdpa",
        torch_dtype=torch.bfloat16,
    )
    model.eval().cuda()
    print("Model loaded.")
    return model


def main() -> None:
    model_dir = resolve_model_dir()
    output_path = OUTPUT_DIR / "duplex_speech_in_chat.mp4"
    video_path = model_dir / "assets" / "speech_in.mp4"
    OUTPUT_DIR.mkdir(exist_ok=True)

    model = load_model(model_dir).as_duplex()
    model.prepare()

    print(f"Extracting per-second audio and frames from {video_path} ...")
    frames, audios, _ = get_video_frame_audio_segments(
        str(video_path),
        stack_frames=1,
        use_ffmpeg=True,
        adjust_audio_length=True,
    )
    print(f"Streaming {len(audios)} seconds; the spoken question is already in the audio track.")

    results, output_audio = [], []
    for second, (frame, audio) in enumerate(zip(frames, audios), start=1):
        model.streaming_prefill(
            audio_waveform=audio,
            frame_list=[frame] if frame is not None else None,
        )
        result = model.streaming_generate()
        print(
            f"[{second}/{len(audios)}]",
            "listen..." if result["is_listen"] else f"speak> {result['text']}",
            flush=True,
        )
        results.append({"chunk_idx": second - 1, **result})
        if result["audio_waveform"] is not None:
            output_audio.append((second - 1, result["audio_waveform"]))

    model.as_simplex()
    print(f"Muxing the spoken responses into {output_path} ...")
    generate_duplex_video(
        video_path=str(video_path),
        output_video_path=str(output_path),
        results_log=results,
        timed_output_audio=output_audio,
    )


if __name__ == "__main__":
    main()
