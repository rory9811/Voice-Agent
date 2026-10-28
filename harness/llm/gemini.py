"""Official Gemini GenerateContent transport; no company relay or tools."""
from __future__ import annotations

import asyncio
import base64
import json
import time

import httpx

from harness.config import validate_gemini_model
from harness.core.models import BackendResponse

OFFICIAL_API = "https://generativelanguage.googleapis.com/v1beta/models"
MAX_REQUEST_BYTES = 20_000_000


class GeminiBackend:
    provider_name = "gemini-official"

    def __init__(self, *, api_key, model, timeout_s=180, transport=None):
        validate_gemini_model(model)
        if not api_key:
            raise ValueError("Official Gemini API key is not configured")
        self._api_key = api_key
        self.model_name = model
        self.timeout_s = timeout_s
        self._transport = transport  # Injectable HTTP transport for offline contract tests.

    async def complete(self, instructions, context):
        return await self.generate(instructions, [{"text": json.dumps(context, ensure_ascii=False)}])

    async def generate(self, instructions, parts):
        body = {
            "systemInstruction": {"parts": [{"text": instructions}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        encoded = json.dumps(body, ensure_ascii=False).encode()
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("Gemini media request exceeds 20 MB; shorten the media window")
        try:
            async with asyncio.timeout(self.timeout_s):
                # Fixed Google origin, no redirects, and no implicit environment proxy.
                async with httpx.AsyncClient(
                    timeout=self.timeout_s, trust_env=False, follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    response = await client.post(
                        f"{OFFICIAL_API}/{self.model_name}:generateContent",
                        headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
                        content=encoded,
                    )
        except (TimeoutError, httpx.TimeoutException):
            raise RuntimeError("Official Gemini request timed out") from None
        except httpx.HTTPError:
            raise RuntimeError("Cannot connect to the official Gemini API") from None
        if not response.is_success:
            # Do not expose request headers or Google's body (which can echo input).
            detail = {
                400: "check model, key and input format", 401: "check API key",
                403: "check key permissions and region", 404: "check model availability",
                429: "quota or rate limit reached",
            }.get(response.status_code, "service request failed")
            raise RuntimeError(f"Official Gemini HTTP {response.status_code}: {detail}")
        try:
            result = response.json()
            candidates = result.get("candidates", [])
            if not candidates:
                raise ValueError("missing candidate")
            candidate = candidates[0]
            if candidate.get("finishReason") != "STOP":
                raise ValueError("incomplete or blocked response")
            text = "".join(
                part["text"] for part in candidate.get("content", {}).get("parts", [])
                if isinstance(part.get("text"), str) and not part.get("thought")
            ).strip()
            if not isinstance(json.loads(text), dict):
                raise ValueError("expected JSON object")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise RuntimeError("Official Gemini returned no complete JSON answer (blocked, truncated or invalid)") from None
        return text


def inline_part(asset):
    return {"inlineData": {"mimeType": asset.mime_type,
                           "data": base64.b64encode(asset.data).decode("ascii")}}


class GeminiDirect:
    def __init__(self, backend):
        self.backend = backend

    async def execute(self, request, context):
        started = time.monotonic()
        assets = []
        if context.audio:
            assets.append(context.audio)
        if context.video:
            if context.video.segment:
                assets.append(context.video.segment)
            else:
                assets.extend(context.video.frames)
        assets.extend(f for f in context.snapshot.input_files if f.mime_type.startswith("image/"))
        # Reject before base64 allocation; the encoded body is checked separately.
        if sum(len(asset.data) for asset in assets) > MAX_REQUEST_BYTES * 3 // 4:
            raise ValueError("Gemini media request is too large; shorten the media window")
        parts = [{"text": json.dumps({"query": request.query, "language": request.language}, ensure_ascii=False)}]
        parts.extend(inline_part(asset) for asset in assets)
        raw = await self.backend.generate(
            'Answer the user using only the supplied text and media. Do not run tools. '
            'Return JSON {"speech":"answer in the requested language"}. '
            'Describe uncertainty when evidence is incomplete.', parts)
        speech = json.loads(raw).get("speech")
        if not isinstance(speech, str) or not speech.strip():
            raise ValueError("Gemini multimodal returned empty speech")
        return BackendResponse(speech, self.backend.provider_name, self.backend.model_name,
                               int((time.monotonic() - started) * 1000))

    async def aclose(self):
        pass
