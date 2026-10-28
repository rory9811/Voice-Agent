"""Per-session turn-ending control without modifying checkpoint source files.

Prefer native support. Released duplex checkpoints without length_penalty use
the compatible sampler below. Only turn_eos is adjusted, after the independent
chunk_eos decision and repetition penalty, before listen/top-k/top-p selection.
TTS, delegate filtering, KV handling and token histories remain model-owned.
"""

import inspect
from functools import partial

from .settings import DuplexSettings


def configure_length_penalty(official, value):
    """Return generation kwargs and the implementation used for this session.

    A decoder override belongs to this model instance, never its shared class.
    Reconfiguration unwraps the previous override, so penalties cannot stack.
    """
    DuplexSettings(length_penalty=value)
    if "length_penalty" in inspect.signature(official.streaming_generate).parameters:
        return {"length_penalty": value}, "native_generation"

    decoder = getattr(official, "decoder", None)
    if decoder is None:
        if value == 1.0:
            return {}, "unchanged"
        raise ValueError("This checkpoint has no configurable duplex decoder")
    original = getattr(decoder, "_venus_original_decode", decoder.decode)
    parameters = inspect.signature(original).parameters
    if "length_penalty" in parameters:
        replacement = partial(original, length_penalty=value)
        implementation = "native_decoder"
    elif value == 1.0:
        decoder.decode = original
        return {}, "unchanged"
    else:
        expected = {
            "logits",
            "mode",
            "temperature",
            "top_k",
            "top_p",
            "listen_top_k",
            "listen_prob_scale",
            "text_repetition_penalty",
            "text_repetition_window_size",
        }
        required = (
            "chunk_eos_id",
            "turn_eos_id",
            "listen_id",
            "forbidden_token_ids",
            "generated_tokens",
            "generated_special_tokens",
            "special_token_ids",
            "tokenizer",
            "context",
        )
        # Checkpoint methods may be wrapped in torch.no_grad(). The sampler
        # helper lives in the underlying model module, not torch's wrapper.
        filtering = getattr(inspect.unwrap(original), "__globals__", {}).get(
            "top_k_top_p_filtering"
        )
        if (
            set(parameters) != expected
            or not all(hasattr(decoder, name) for name in required)
            or not callable(filtering)
        ):
            raise ValueError("This checkpoint's duplex decoder is not supported")
        import torch

        replacement = torch.no_grad()(
            partial(_decode_legacy, decoder, length_penalty=value, filtering=filtering)
        )
        implementation = "compatible_decoder"
    decoder._venus_original_decode = original
    decoder.decode = replacement
    return {}, implementation


def _decode_legacy(
    decoder,
    logits,
    mode="sampling",
    temperature=0.7,
    top_k=20,
    top_p=0.8,
    listen_top_k=None,
    listen_prob_scale=1.0,
    text_repetition_penalty=1.05,
    text_repetition_window_size=512,
    *,
    length_penalty,
    filtering,
):
    """Preserve the released sampler's ordering and history updates."""
    import torch
    import torch.nn.functional as F

    logits = logits.clone()
    # Do not bias the separate decision to end this one-second chunk.
    with torch.no_grad():
        if mode == "greedy":
            sampled = torch.argmax(logits[0]).item()
        else:
            sampled = torch.multinomial(F.softmax(logits[0], dim=-1), 1).item()
        if sampled == decoder.chunk_eos_id:
            return torch.tensor([decoder.chunk_eos_id], device=logits.device)

    if decoder.forbidden_token_ids:
        logits[:, decoder.forbidden_token_ids] = float("-inf")
    if text_repetition_penalty != 1.0 and decoder.generated_tokens:
        recent = set(decoder.generated_tokens[-text_repetition_window_size:])
        for token_id in recent:
            if token_id < logits.size(-1):
                if text_repetition_penalty > 1.0:
                    logits[0, token_id] /= text_repetition_penalty
                else:
                    logits[0, token_id] *= 1.0 / text_repetition_penalty

    # Match the duplex reference: <1 encourages turn_eos for either sign.
    if length_penalty != 1.0:
        eos = decoder.turn_eos_id
        if logits[0, eos] > 0:
            logits[0, eos] /= length_penalty
        else:
            logits[0, eos] *= length_penalty

    if listen_prob_scale != 1.0:
        logits[0, decoder.listen_id] *= listen_prob_scale
    listen_rank = (logits[0] > logits[0, decoder.listen_id]).sum().item()
    if listen_top_k is not None and listen_rank < listen_top_k:
        token = torch.tensor([decoder.listen_id], device=logits.device)
        text = decoder.tokenizer.decode(token)
        decoder.context += " " if text == "<|listen|>" else text
        return token

    if mode == "greedy":
        token = torch.argmax(logits, dim=-1)
    elif mode == "sampling":
        logits = filtering(logits / temperature, top_k=top_k, top_p=top_p)
        token = torch.multinomial(F.softmax(logits, dim=-1), 1).squeeze(1)
    else:
        raise ValueError(f"Unsupported decode mode: {mode}")
    history = (
        decoder.generated_special_tokens
        if token.item() in decoder.special_token_ids
        else decoder.generated_tokens
    )
    history.append(token.item())
    return token
