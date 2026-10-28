"""Hard limits for spoken summaries; full artifacts and raw results stay intact."""

import re


def limit_spoken_sentences(text, maximum=3):
    text = re.sub(r"```[\s\S]*?```", "", str(text)).strip()
    # Count newline-separated list items too; retain decimals and filenames.
    text = re.sub(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+", "", text)
    pieces = [part.strip() for part in re.split(
        r"(?<=[。！？!?])\s*|(?<=\.)\s+|[\r\n]+", text
    ) if part.strip()]
    return " ".join(pieces[:maximum])


def brief_progress(text, language="zh", fallback=None):
    """Reject diagnostic narration instead of trimming it into a misleading claim."""
    fallback = fallback or (
        "I'm still working on it." if language == "en" else "我还在处理，有结果就告诉你。"
    )
    if re.search(
        r"进度反馈|阶段进度|命令执行|已观察到|尚未确认|没有新的阶段|"
        r"exit.?code|command execution|observed.*(?:command|tool)|no new.*progress",
        text, re.I,
    ):
        return fallback
    short = limit_spoken_sentences(text, 1)
    limit = 160 if language == "en" else 50
    if not short or len(short) > limit or (language == "en" and len(short.split()) > 25):
        return fallback
    return short


def spoken_observation(observed):
    """Keep task-level commentary, not command counts, exit codes or internal IDs.

    Commentary describes the worker's current activity, not verified completion.
    Raw observations remain available through the existing diagnostic interface.
    """
    if observed is None:
        return None
    return {"worker_updates": [
        str(item.get("text", ""))[:400]
        for item in observed.get("items", [])
        if item.get("type") == "publicCommentary" and item.get("text")
    ][-3:]}
