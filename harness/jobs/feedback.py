"""Work-owned feedback policy, safe local rendering and durable receipts."""

from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from harness.core.models import DelegateResult
from harness.core.speech import limit_spoken_sentences

from .messages import local_text
from .models import WorkState


@dataclass(frozen=True)
class FeedbackConfig:
    proactive_progress: bool = False
    progress_interval_s: float = 30
    progress_timeout_s: float = 15
    read_retry_s: float = 30
    first_notice_s: float = 30
    repeat_notice_s: float = 60
    max_repeat_s: float = 120
    poll_s: float = 0.25
    notice_ttl_s: float = 60
    queue_timeout_s: float = 300
    skill_timeout_s: float = 300
    stage_timeout_s: float = 180
    journal_path: str | None = None

    def __post_init__(self):
        if type(self.proactive_progress) is not bool:
            raise ValueError("proactive_progress must be a boolean")
        for name in (
            "progress_interval_s",
            "progress_timeout_s",
            "read_retry_s",
            "first_notice_s",
            "repeat_notice_s",
            "max_repeat_s",
            "poll_s",
            "notice_ttl_s",
            "queue_timeout_s",
            "skill_timeout_s",
            "stage_timeout_s",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")

    @classmethod
    def from_env(cls):
        return cls(
            progress_interval_s=float(os.getenv("WORK_PROGRESS_INTERVAL_S", "30")),
            progress_timeout_s=float(os.getenv("WORK_PROGRESS_TIMEOUT_S", "15")),
            read_retry_s=float(os.getenv("WORK_READ_RETRY_S", "30")),
            first_notice_s=float(os.getenv("WORK_FIRST_NOTICE_S", "30")),
            journal_path=os.getenv("WORK_JOURNAL_PATH") or None,
        )


class WorkJournal:
    """No execution replay on recovery. A saved receipt is not proof of playback."""

    def __init__(self, path):
        self.error = None
        self.db = None
        self.recovered = []
        try:
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, timeout=1)
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS works (id TEXT PRIMARY KEY, snapshot TEXT NOT NULL)"
            )
            for (raw,) in self.db.execute("SELECT snapshot FROM works"):
                value = json.loads(raw)
                value.pop("request_id", None)
                value.pop("run_id", None)
                value["recovered"] = True
                if value["state"] in {"queued", "running", "cancelling"}:
                    value.update(state="interrupted", execution_outcome="unknown")
                if value.get("delivery_status") in {"pending", "delivering"}:
                    value["delivery_status"] = "unknown"
                self.recovered.append(value)
        except (OSError, sqlite3.Error, ValueError):
            self.error = "Work 状态存储不可用；本次状态仅保存在内存中。"

    def save(self, work):
        if self.db is None:
            return
        try:
            snapshot = {
                **work.snapshot(),
                "result": work.result,
                "feedback_records": work.feedback_records,
            }
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO works VALUES (?, ?)",
                    (work.work_id, json.dumps(snapshot, ensure_ascii=False)),
                )
        except (OSError, sqlite3.Error, TypeError, ValueError):
            self.error = "Work 状态保存失败；已保留内存中的任务结果。"

    def close(self):
        if self.db:
            self.db.close()
            self.db = None


class WorkFeedbackController:
    def __init__(self, works, config, journal):
        self.works, self.config, self.journal = works, config, journal
        self._signals = {}
        self._pending = {}
        self._last = {}
        self._poll_last = {}
        self._count = {}
        self._fingerprints = {}
        self._phase_times = {}
        self.health = {}
        self.reporter = None

    def changed(self, work):
        work.revision += 1
        work.updated_at = time.time()
        self.journal.save(work)
        self._signals.setdefault(work.work_id, asyncio.Event()).set()

    def forget_session(self, session_id):
        for work in self.works.works.values():
            if work.session_id != session_id:
                continue
            for mapping in (
                self._signals,
                self._pending,
                self._last,
                self._poll_last,
                self._count,
                self._fingerprints,
                self._phase_times,
            ):
                mapping.pop(work.work_id, None)
            for record in work.feedback_records:
                if record["delivery_status"] in {"pending", "delivering"}:
                    record["delivery_status"] = "unknown"
            self.journal.save(work)

    def delivered(self, work_id, feedback_id, status):
        work = self.works.get(work_id)
        if not work:
            return
        record = next(
            (r for r in work.feedback_records if r["feedback_id"] == feedback_id), None
        )
        if record:
            record["delivery_status"] = status
            if status == "delivered":
                work.last_feedback_at = time.time()
                self._last[work_id] = time.monotonic()
                if self._pending.get(work_id) == feedback_id:
                    self._pending.pop(work_id, None)
            if record["terminal"]:
                work.delivery_status = status
            self.journal.save(work)

    def valid(self, result):
        if not result.feedback_id:
            return True
        work = self.works.get(result.work_id)
        if work is None or work.session_id != result.session_id:
            return False
        if result.status != "pending":
            return (
                work.state not in {WorkState.CANCELLING, WorkState.CANCELLED}
                or result.metadata.get("kind") == "cancelled"
            )
        return (
            work.state in {WorkState.QUEUED, WorkState.RUNNING}
            and result.metadata.get("work_revision") == work.revision
            and time.time_ns() // 1_000_000 <= result.expires_at_ms
        )

    def describe(self, work):
        language = work.language
        if work.fault and work.phase == "retry_wait":
            return work.fault["message"] + local_text("已安排有限重试。", language)
        # State takes precedence over a report from an earlier execution phase.
        defaults = {
            "queued": "还在排队，轮到后就开始处理。",
            "routing": "我先确认一下怎么处理。",
            "waiting_agent": "等上一项做完，就接着处理这项。",
            "polish": "结果有了，我整理一下就告诉你。",
            "multimodal": "我正在看你提供的内容。",
            "skill": "我还在处理，有结果就告诉你。",
        }
        if work.phase in defaults:
            return local_text(defaults[work.phase], language)
        if work.progress:
            return work.progress["text"]
        return local_text("我还在处理，有结果就告诉你。", language)

    def make_notice(self, request, work, text=None):
        now = time.time_ns() // 1_000_000
        kind = work.progress.get("kind", "progress")
        identifier = f"{work.work_id}:feedback:{uuid4().hex}"
        result = DelegateResult(
            work_id=work.work_id,
            session_id=work.session_id,
            status="pending",
            spoken_text=text if text is not None else self.describe(work),
            raw_text=text if text is not None else self.describe(work),
            created_at_ms=now,
            completed_at_ms=now,
            expires_at_ms=now + int(self.config.notice_ttl_s * 1000),
            feedback_id=identifier,
            metadata={
                "kind": kind,
                "language": work.language,
                "work_revision": work.revision,
                "managed_feedback": True,
                "terminal": False,
            },
        )
        self._record(work, result)
        self._pending[work.work_id] = identifier
        return result

    def _record(self, work, result):
        work.feedback_records.append(
            {
                "feedback_id": result.feedback_id,
                "kind": result.metadata.get("kind", "final"),
                "terminal": result.status != "pending",
                "text": result.spoken_text,
                "delivery_status": "pending",
                "revision": work.revision,
                "at": time.time(),
            }
        )
        # Bound ordinary history; terminal records remain in the journal's current snapshot.
        del work.feedback_records[:-64]
        self.journal.save(work)

    def terminal(self, result):
        work = self.works.get(result.work_id)
        if not work:
            return result
        self._pending.pop(work.work_id, None)
        kind = "error" if work.state == WorkState.FAILED else "final"
        if work.state == WorkState.CANCELLED:
            kind = "cancelled"
        text = result.spoken_text
        if work.fault and work.execution_outcome not in {"completed", "partial"}:
            text = work.fault["message"]
        result = replace(
            result,
            feedback_id=result.work_id,
            spoken_text=limit_spoken_sentences(text, 3),
            metadata={
                **result.metadata,
                "managed_feedback": True,
                "kind": kind,
                "terminal": True,
                "work_revision": work.revision,
                "outcome": work.execution_outcome,
                "stale": False,
                "source_stale": bool(result.metadata.get("stale")),
            },
            expires_at_ms=max(result.expires_at_ms, result.completed_at_ms + 300_000),
        )
        work.delivery_status = "pending"
        self._record(work, result)
        return result

    async def watch(self, request, publish):
        # Consent gates automatic feedback only; queries and final results stay available.
        if not self.config.proactive_progress:
            return
        key = request.work_id
        self._last[key] = time.monotonic()
        signal = self._signals.setdefault(key, asyncio.Event())
        while True:
            work = self.works.get(key)
            if not work or work.state not in {WorkState.QUEUED, WorkState.RUNNING}:
                return
            if work.capability == "progress_query":
                return
            if work.capability == "general" and self.reporter:
                if work.execution_outcome in {"completed", "partial"}:
                    return
                if self.reporter.active_queries.get((work.session_id, key)):
                    await asyncio.sleep(self.config.poll_s)
                    continue
                interval = self.config.progress_interval_s
                self._poll_last.setdefault(key, time.monotonic())
                retry_at = self.reporter.retry_at.get((work.session_id, key))
                due = (
                    time.monotonic() >= retry_at
                    if retry_at is not None
                    else time.monotonic() - self._poll_last[key] >= interval
                )
                if due:
                    self._poll_last[key] = time.monotonic()
                    generation = self.reporter.query_generation.get(key, 0)
                    text = await self.reporter.text(request, work, proactive=True)
                    if (
                        text is None
                        or work.state not in {WorkState.QUEUED, WorkState.RUNNING}
                        or work.phase == "polish"
                        or generation != self.reporter.query_generation.get(key, 0)
                    ):
                        continue
                    self.changed(work)
                    notice = self.make_notice(request, work, text)
                    try:
                        await publish(notice)
                    except Exception:  # noqa: BLE001 - feedback delivery must not fail execution
                        self.delivered(key, notice.feedback_id, "failed")
                        self._pending.pop(key, None)
                await asyncio.sleep(self.config.poll_s)
                continue
            observed_phase, phase_started = self._phase_times.get(
                key, (None, time.monotonic())
            )
            if observed_phase != work.phase:
                phase_started = time.monotonic()
                self._phase_times[key] = (work.phase, phase_started)
            count = self._count.get(key, 0)
            interval = (
                self.config.first_notice_s
                if not count
                else min(
                    self.config.repeat_notice_s * 2 ** min(count - 1, 4),
                    self.config.max_repeat_s,
                )
            )
            immediate = bool(work.progress.get("kind") in {"important", "correction"})
            fingerprint = (work.phase, work.progress.get("text"))
            pending = self._pending.get(key)
            record = next(
                (r for r in work.feedback_records if r["feedback_id"] == pending), None
            )
            if record and (
                record["revision"] != work.revision
                or time.time() - record["at"] >= self.config.notice_ttl_s
            ):
                self._pending.pop(key, None)
            last_fingerprint = self._fingerprints.get(key)
            if work.progress and fingerprint != last_fingerprint:
                interval = min(interval, self.config.first_notice_s)
            if (
                (
                    work.phase != "polish"
                    or time.monotonic() - phase_started >= self.config.first_notice_s
                )
                and not self._pending.get(key)
                and (
                    time.monotonic() - self._last[key] >= interval
                    or (immediate and fingerprint != last_fingerprint)
                )
            ):
                revision = work.revision
                text = (
                    await self.reporter.text(request, work, proactive=True)
                    if self.reporter
                    else self.describe(work)
                )
                if (
                    text is None
                    or revision != work.revision
                    or work.state not in {WorkState.QUEUED, WorkState.RUNNING}
                ):
                    continue
                notice = self.make_notice(request, work, text)
                try:
                    await publish(notice)
                except Exception:  # noqa: BLE001 - progress transport cannot terminate execution
                    self.delivered(key, notice.feedback_id, "failed")
                    self._pending.pop(key, None)
                self._last[key] = time.monotonic()
                self._count[key] = count + 1
                self._fingerprints[key] = fingerprint
            signal.clear()
            try:
                await asyncio.wait_for(signal.wait(), self.config.poll_s)
            except TimeoutError:
                pass
