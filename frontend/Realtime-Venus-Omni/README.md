# Realtime-Venus-Omni

This directory contains five standalone examples for running
[Realtime-Venus-Omni](https://huggingface.co/inclusionAI/Realtime-Venus)
with Hugging Face Transformers. The examples cover streaming full-duplex and
turn-based half-duplex inference, both with and without long-video Memory.

## Examples

| Example | Mode | Input question | Output |
| --- | --- | --- | --- |
| [`duplex_chat.py`](./duplex_chat.py) | Full-duplex | Text injected at 60 s and 128 s | `output/duplex_chat.mp4` |
| [`duplex_speech_in_chat.py`](./duplex_speech_in_chat.py) | Full-duplex | Speech already mixed into the video | `output/duplex_speech_in_chat.mp4` |
| [`duplex_memory_chat.py`](./duplex_memory_chat.py) | Full-duplex + Memory | Text injected at 128 s | `output/duplex_memory_chat.mp4` |
| [`offline_chat.py`](./offline_chat.py) | Half-duplex | Text appended after the video | Text and `output/offline_chat.wav` |
| [`offline_memory_chat.py`](./offline_memory_chat.py) | Half-duplex + Memory | Text appended after the video | Text and `output/offline_memory_chat.wav` |

## Setup

Python 3.10, CUDA, and FFmpeg are required. Run all commands from the source
repository root. Install the download helper's dependencies, then select Omni:

```bash
python -m pip install 'huggingface_hub>=0.34' 'PyYAML>=6.0'
python download_models.py --model omni --local-dir .
```

The downloader reads the Hugging Face repository's root `config.yaml` and
saves it alongside the complete `Realtime-Venus-Omni/` directory. It displays
standard Hub progress bars and reuses cached files. See the
[root guide](../../README.md#quick-start) for the `audio` and `all` selections
and the Python download API.

To use the optional ModelScope mirror, download Omni directly with its CLI:

```bash
python -m pip install modelscope
modelscope download --model inclusionAI/Realtime-Venus --local_dir . \
  --include "Realtime-Venus-Omni/*"
```

After downloading, install the model dependencies:

```bash
python -m pip install -r Realtime-Venus-Omni/requirements.txt
```

Each example resolves the local model in this order:

1. `REALTIME_VENUS_MODEL_PATH`, when set.
2. The `Realtime-Venus-Omni/` model directory at the repository root.

The download command above uses the second location. To select an existing
local model directory elsewhere explicitly:

```bash
export REALTIME_VENUS_MODEL_PATH=/path/to/Realtime-Venus-Omni
```

## Run

Run every example in a fresh Python process from the repository root:

```bash
python frontend/Realtime-Venus-Omni/duplex_chat.py
python frontend/Realtime-Venus-Omni/duplex_speech_in_chat.py
python frontend/Realtime-Venus-Omni/duplex_memory_chat.py
python frontend/Realtime-Venus-Omni/offline_chat.py
python frontend/Realtime-Venus-Omni/offline_memory_chat.py
```

Input videos are read from the resolved model directory's `assets/` folder.
Outputs are written to `frontend/Realtime-Venus-Omni/output/`, with filenames
matching the examples. The examples use `set_seed(42)` for
reproducible sampling and load the checkpoint in BF16 with SDPA attention.

Duplex examples burn response subtitles into the output video through
FFmpeg/libass. Install a CJK-capable font when rendering non-Latin text; see the
main model card for font-installation commands and complete API documentation.
