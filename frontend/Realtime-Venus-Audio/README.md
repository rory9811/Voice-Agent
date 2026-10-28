# Realtime-Venus-Audio

Audio-only inference examples for Realtime-Venus-Audio.

| Example | Behavior | Output |
| --- | --- | --- |
| [`audio_offline_chat.py`](./audio_offline_chat.py) | Processes the complete input with deterministic `model.chat` inference | Text on stdout |
| [`audio_duplex_chat.py`](./audio_duplex_chat.py) | Feeds one-second chunks through `as_duplex()` while the model alternates between listening and speaking | Text on stdout and a 24 kHz WAV file |

## Setup

Python 3.10, CUDA, and FFmpeg are required. Run all commands from the source
repository root. Install the download helper's dependencies, then select Audio:

```bash
python -m pip install 'huggingface_hub>=0.34' 'PyYAML>=6.0'
python download_models.py --model audio --local-dir .
```

The downloader reads the Hugging Face repository's root `config.yaml` and
saves it alongside the complete `Realtime-Venus-Audio/` directory. It displays
standard Hub progress bars and reuses cached files. See the
[root guide](../../README.md#quick-start) for the `omni` and `all` selections
and the Python download API.

To use the optional ModelScope mirror, download Audio directly with its CLI:

```bash
python -m pip install modelscope
modelscope download --model inclusionAI/Realtime-Venus --local_dir . \
  --include "Realtime-Venus-Audio/*"
```

Install the pinned project dependencies from the repository root:

```bash
python -m pip install -r requirements.txt
```

The pinned `transformers==4.51.3` version is important because the checkpoint's
remote model code uses that cache and attention API. FFmpeg is also required
when the input is a video or another format that `librosa` cannot decode
directly.

The model path must be a local checkpoint directory containing `config.json`
and the model weight shards. The download command above creates
`./Realtime-Venus-Audio/`; use that directory for the `--model-path` examples
below. Both examples load the model in bfloat16 on CUDA.
The streaming example additionally loads the TTS module and therefore uses
more GPU memory.

## Offline Dialogue

Run the bundled offline sample:

```bash
python frontend/Realtime-Venus-Audio/audio_offline_chat.py \
  --model-path /path/to/Realtime-Venus-Audio \
  --audio frontend/Realtime-Venus-Audio/case/case_offline.wav
```

`--model-path` is optional for offline inference. The script resolves the model
in this order:

1. `--model-path`.
2. `REALTIME_VENUS_MODEL_PATH`.
3. `Realtime-Venus-Audio/` at the repository root when it contains `config.json`.
4. The shared downloader reads `inclusionAI/Realtime-Venus/config.yaml` and
   downloads only Audio into the repository's `Realtime-Venus-Audio/` directory.

When `--audio` is omitted, the script uses `assets/speech_in.mp4` inside the
resolved model directory. Pass `--audio` explicitly when that asset is not
included in the checkpoint.

Offline decoding is deterministic by default. Use `--num-beams` and
`--max-new-tokens` to control beam search and response length.

## Streaming Dialogue

Run the bundled full-duplex sample:

```bash
python frontend/Realtime-Venus-Audio/audio_duplex_chat.py \
  --model-path /path/to/Realtime-Venus-Audio \
  --audio frontend/Realtime-Venus-Audio/case/case_duplex.wav \
  --output output/duplex_response.wav
```

`--model-path`, `--audio`, and `--output` are required. The input is decoded as
16 kHz mono audio and sent to the model in one-second chunks. The script appends
10 seconds of silence so the model can finish responding after the input ends.
Generated speech is placed on the original streaming timeline; listening
intervals remain silent in the output WAV.

The default decoding mode is sampling. It can be adjusted with
`--decode-mode`, `--max-new-tokens-per-chunk`, `--temperature`, `--top-k`,
`--top-p`, and `--listen-prob-scale`.

## Prompts

Both examples accept `--system-prompt` and an optional `--prompt`. Offline mode
appends the prompt after the input audio. Streaming mode injects it with the
final chunk, after the appended silence.

Use `python <script> --help` for the complete CLI reference.
