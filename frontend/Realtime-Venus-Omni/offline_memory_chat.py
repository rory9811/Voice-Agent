"""Run half-duplex Omni chat inference with long-video Memory enabled."""

import os
from pathlib import Path

os.environ["MAX_NUM_FRAMES"] = "100000"

import torch
from minicpmo.utils import get_video_frame_audio_segments
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
    output_path = OUTPUT_DIR / "offline_memory_chat.wav"
    video_path = model_dir / "assets" / "sample_1_real.mp4"
    OUTPUT_DIR.mkdir(exist_ok=True)

    model = load_model(model_dir)
    model.use_memory()
    model.init_tts()

    question = "What is the color of the cooler labeled PRIME near the team bench?"
    print(f"Extracting audio and frames from {video_path} ...")
    frames, audios, _ = get_video_frame_audio_segments(
        str(video_path),
        stack_frames=1,
        use_ffmpeg=True,
        adjust_audio_length=True,
    )
    content = []
    for frame, audio in zip(frames, audios):
        if frame is not None:
            content.append(frame)
        content.append(audio)
    content.append(question)

    print("Running chat inference ...")
    response = model.chat(
        msgs=[{"role": "user", "content": content}],
        max_new_tokens=4096,
        max_inp_length=32768,
        do_sample=True,
        temperature=0.7,
        use_image_id=False,
        max_slice_nums=1,
        use_tts_template=True,
        enable_thinking=False,
        omni_mode=True,
        generate_audio=True,
        output_audio_path=str(output_path),
    )
    print(response)


if __name__ == "__main__":
    main()
