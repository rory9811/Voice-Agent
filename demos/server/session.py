"""One browser, one model session, one model-owner output loop."""

import asyncio
import logging
import math
import secrets
import time
import uuid
from contextlib import suppress
from pathlib import Path

from harness.core.speech import limit_spoken_sentences

import anyio

from demos.model.client import RemoteModelUnavailable
from harness.bridge.host import AudioDisposition, VenusOmniServingHost
from harness.core.models import AudioChunk, VideoFrame
from harness.core.result_manager import audio_rms

from .protocol import decode_media, playback_message

logger = logging.getLogger(__name__)


class BrowserPlayback:
    def __init__(self, send):
        self.send = send

    async def enqueue(self, request):
        await self.send(playback_message(request))


class WebAgentSession:
    def __init__(
        self,
        websocket,
        factory,
        *,
        mode="camera",
        settings_path=None,
    ):
        self.websocket = websocket
        self.factory = factory
        self.mode = mode
        self.settings_path = settings_path
        self.session_id = f"web-{uuid.uuid4().hex}"
        self.upload_token = secrets.token_urlsafe(32)
        self._link_up = True
        self._link_down_at = 0.0
        self._dropped_audio_ms = 0
        self.resources = None
        self.host = None
        self._send_lock = asyncio.Lock()
        self._last_works = None
        self._work_marks = {}
        self._audio_end = 0
        self._last_video_ms = 0
        self._level_energy = 0.0  # sum of rms² × ms over the current second of input
        self._level_ms = 0
        self._level_playing = False
        self.started_at_ms = int(time.time() * 1000)

    async def send(self, message):
        async with self._send_lock:
            await self.websocket.send_json(message)

    async def _link_changed(self, up):
        if not up:
            self._link_down_at = time.monotonic()
            self._dropped_audio_ms = 0
        elif not self._link_up:
            logger.info(
                "model link outage session=%s down_s=%.1f dropped_audio_ms=%d",
                self.session_id,
                time.monotonic() - self._link_down_at,
                self._dropped_audio_ms,
            )
        self._link_up = up
        await self.send({"type": "model_link", "state": "up" if up else "down"})

    async def run(self):
        await self.websocket.accept()
        tasks = []
        try:
            if self.mode not in {"camera", "audio"}:
                raise ValueError("unsupported input mode")
            self.resources = await self.factory(
                mode=self.mode, settings_path=self.settings_path
            )
            if not self.resources.model.lower().startswith("realtime-venus-"):
                raise ValueError("frontend model identifier must use the Venus series")
            self.started_at_ms = int(time.time() * 1000)
            self.resources.serving.on_link_change = self._link_changed
            self.host = await VenusOmniServingHost.open(
                agent=self.resources.agent,
                serving=self.resources.serving,
                playback=BrowserPlayback(self.send),
                session_id=self.session_id,
                model=self.resources.model,
                tokenizer=self.resources.tokenizer,
                mute_delegate_audio=True,
            )
            await self.send({"type": "ready", **self.ui_state()})
            tasks = [
                asyncio.create_task(fn())
                for fn in (self.receive, self.output, self.works)
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except Exception as exc:
            if type(exc).__name__ not in {"WebSocketDisconnect", "ClientDisconnected"}:
                logger.exception("Venus Web session failed")
                failure = {
                    "type": "fatal_error",
                    "error": "Venus 会话启动或运行失败，请在 deploy/web 目录运行 docker compose logs 查看原因。",
                }
                # A host another caller saw fail on the dead link raises its own error from the port's.
                cause = exc
                while cause is not None and not isinstance(cause, RemoteModelUnavailable):
                    cause = cause.__cause__
                if cause is not None:
                    failure.update(code=cause.code, error=str(cause))
                with suppress(Exception):
                    await self.send(failure)
        finally:
            with anyio.CancelScope(shield=True):
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if self.host:
                    with suppress(Exception):
                        await self.host.close()
                if self.resources:
                    with suppress(Exception):
                        await self.resources.close()
                with suppress(Exception):
                    await self.websocket.close()

    def ui_state(self):
        return {
            "model": self.resources.model,
            "harness": "Realtime-Venus-Harness",
            "session_id": self.session_id,
            "session_started_at_ms": self.started_at_ms,
            "upload_token": self.upload_token,
            "delegate_audio_filtered": self.host.opened.capabilities.delegate_audio_filtered,
            "delegate_audio_policy": "mute_until_turn_end",
        }

    def work_snapshot(self):
        # Only session-local public status; private model/tool traces stay server-side.
        snapshots = []
        for work in reversed(self.host.session.list_works()):
            item = {
                key: work.get(key)
                for key in (
                    "work_id",
                    "objective",
                    "capability",
                    "state",
                    "phase",
                    "created_at",
                    "started_at",
                    "completed_at",
                    "updated_at",
                    "queue_position",
                    "execution_outcome",
                    "delivery_status",
                )
            }
            item["progress_text"] = (work.get("progress") or {}).get("text", "")
            item["error_message"] = (work.get("fault") or {}).get("message", "")
            item["feedback"] = [
                {key: record.get(key) for key in ("text", "terminal", "at", "kind")}
                for record in work.get("feedback_records", [])[-20:]
            ]
            result = self.host.session.get_work_result(item["work_id"]) or {}
            item["result_text"] = next(
                (
                    result[key]
                    for key in ("speech", "text", "summary", "full_result")
                    if isinstance(result.get(key), str)
                ),
                "",
            )
            final_feedback = next(
                (record.get("text", "") for record in reversed(work.get("feedback_records", []))
                 if record.get("terminal") and record.get("text")),
                "",
            )
            item["result_text"] = limit_spoken_sentences(final_feedback or item["result_text"], 3)
            item["artifacts"] = [
                {
                    "index": index,
                    "name": Path(artifact["path"]).name,
                    "mime_type": artifact.get("mime_type", "application/octet-stream"),
                }
                for index, artifact in enumerate(result.get("artifacts", []))
            ]
            snapshots.append(item)
        return snapshots

    async def works(self):
        while True:
            snapshot = self.work_snapshot()
            if snapshot != self._last_works:
                self._log_work_changes(snapshot)
                await self.send({"type": "works", "works": snapshot})
                self._last_works = snapshot
            await asyncio.sleep(0.25)

    def _log_work_changes(self, snapshot):
        """One line per task change; the objective and results stay out of the log."""
        for work in snapshot:
            feedback = work["feedback"][-1] if work["feedback"] else {}
            mark = (work["state"], work["phase"], work["delivery_status"], feedback.get("at"))
            if self._work_marks.get(work["work_id"]) == mark:
                continue
            self._work_marks[work["work_id"]] = mark
            logger.info(
                "work session=%s id=%s capability=%s state=%s phase=%s delivery=%s "
                "last_feedback=%s age_s=%.1f",
                self.session_id,
                work["work_id"],
                work["capability"],
                work["state"],
                work["phase"],
                work["delivery_status"],
                feedback.get("kind", "-"),
                time.time() - (work["created_at"] or time.time()),
            )

    async def output(self):
        finished = True
        queued = suppressed = chars = 0
        while True:
            if finished and await self.host.begin_backend_turn():
                finished = False
            try:
                processed = await self.host.next_output(timeout_s=1.0)
            except (TimeoutError, asyncio.TimeoutError):
                continue
            finished = processed.output.turn_finished
            queued += processed.audio_disposition is AudioDisposition.QUEUED
            suppressed += processed.audio_disposition is AudioDisposition.SUPPRESSED
            chars += len(processed.model_step.visible_text)
            if finished:
                if queued or suppressed or chars:  # one-second listen units say nothing
                    logger.info(
                        "turn session=%s gen=%s work=%s queued=%d suppressed=%d chars=%d",
                        self.session_id,
                        processed.output.generation_id,
                        processed.caused_by_work_id or "-",
                        queued,
                        suppressed,
                        chars,
                    )
                queued = suppressed = chars = 0
            # Text attached to audio is emitted by BrowserPlayback, only after the gate.
            if (
                processed.model_step.visible_text
                and processed.audio_disposition is not AudioDisposition.QUEUED
            ):
                await self.send(
                    {
                        "type": "text",
                        "text": processed.model_step.visible_text,
                        "utterance_id": processed.utterance_id,
                    }
                )

    async def receive(self):
        while True:
            message = await self.websocket.receive_json()
            command = message.get("type", "") if isinstance(message, dict) else ""
            if command == "stop_session":
                return
            try:
                if not isinstance(message, dict):
                    raise TypeError("消息必须是 JSON object")
                await self.handle(command, message)
            except (TypeError, ValueError, RuntimeError, KeyError) as exc:
                await self.send(
                    {"type": "command_error", "command": command, "error": str(exc)}
                )

    async def handle(self, command, message):
        now = int(time.time() * 1000)
        if command in {"audio", "video_frame"} and not self._link_up:
            if command == "audio":
                self._dropped_audio_ms += len(decode_media(message, kind="audio")) // 32
            return
        if command == "audio":
            if message.get("format") != "pcm16" or message.get("sample_rate") != 16000:
                raise ValueError("需要 16000Hz 单声道 PCM16")
            data = decode_media(message, kind="audio")
            duration = max(1, round(len(data) / 32))
            start = max(self._audio_end, now - duration)
            chunk = AudioChunk(data=data, start_ms=start, end_ms=start + duration)
            await self.host.append_audio(chunk)
            self._audio_end = start + duration
            self._log_level(chunk, message.get("playing") is True)
        elif command == "video_frame":
            if self.mode != "camera":
                raise ValueError("音频双工不接收画面")
            await self.host.append_video_frame(
                VideoFrame(
                    data=decode_media(message, kind="video_frame"), captured_at_ms=now
                )
            )
            self._last_video_ms = now
        elif command == "playback_ack":
            count = message.get("chunks")
            if type(count) is not int or count < 1:
                raise ValueError("播放确认必须包含正整数 chunks")
            await self.host.acknowledge_playback(
                str(message.get("utterance_id", "")), count
            )
        elif command == "cancel_work":
            result = await self.host.session.cancel_work(
                str(message.get("work_id", ""))
            )
            await self.send({"type": "cancel_result", "result": result})
        elif command == "get_works":
            await self.send({"type": "works", "works": self.work_snapshot()})
        elif command == "get_ui_state":
            await self.send({"type": "ready", **self.ui_state()})
        elif command == "ping":
            await self.send({"type": "pong"})
        else:
            raise ValueError("未知 Web 命令")

    def _log_level(self, chunk, playing):
        """One line per second of live input: its loudness, and whether model audio was audible."""
        rms = audio_rms(chunk) or 0.0
        self._level_energy += rms * rms * (chunk.end_ms - chunk.start_ms)
        self._level_ms += chunk.end_ms - chunk.start_ms
        self._level_playing |= playing
        if self._level_ms < 1000:
            return
        level = math.sqrt(self._level_energy / self._level_ms)
        logger.info(
            "input session=%s t_s=%d rms_dbfs=%.1f playing=%s",
            self.session_id,
            (self._audio_end - self.started_at_ms) // 1000,
            20 * math.log10(max(level, 1.0) / 32768),
            self._level_playing,
        )
        self._level_energy, self._level_ms, self._level_playing = 0.0, 0, False
