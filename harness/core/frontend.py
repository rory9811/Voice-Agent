"""Assemble one private, model-facing message for each delegate result."""

from __future__ import annotations

from typing import Any, Dict

from .models import DelegateResult
from .speech import limit_spoken_sentences


def assemble_frontend_message(result: DelegateResult) -> Dict[str, Any]:
    """Return the only delegate payload the frontend model needs to read.

    The session runner guarantees ``spoken_text`` for completed, pending, and
    failed results. The defensive fallback keeps this delivery boundary useful
    for results constructed by older integrations as well.
    """

    text = limit_spoken_sentences(result.spoken_text, 3) or "抱歉，刚才的问题暂时没能处理完成。"
    message = {
        "type": "delegate_result",
        "work_id": result.work_id,
        "text": text,
    }
    if result.feedback_id:
        message.update(
            feedback_id=result.feedback_id, kind=result.metadata.get("kind", "final")
        )
    return message
