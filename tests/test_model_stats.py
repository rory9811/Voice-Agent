import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from demos.model.adapter import gpu_memory
from demos.model.server import _ServerConfig, create_app
from demos.model.wire import PROTOCOL_VERSION, StepStats
from test_model_server import omni_adapter

MIB = 1024 * 1024


class StepStatsTests(unittest.TestCase):
    def test_percentiles_over_recent_steps(self):
        stats = StepStats(size=100)
        for ms in range(1, 101):
            stats.add(ms)
        self.assertEqual(stats.summary(), {"count": 100, "p50_ms": 50, "p95_ms": 95, "max_ms": 100})

    def test_window_keeps_only_recent_steps(self):
        stats = StepStats(size=10)
        for ms in range(1, 101):
            stats.add(ms)
        self.assertEqual(stats.summary()["count"], 10)
        self.assertEqual(stats.summary()["p50_ms"], 95)

    def test_empty(self):
        self.assertEqual(StepStats().summary(), {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None})


class GpuMemoryTests(unittest.TestCase):
    def test_none_without_cuda(self):
        fake = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
        with patch.dict(sys.modules, {"torch": fake}):
            self.assertIsNone(gpu_memory())

    def test_reports_process_and_device_mib(self):
        cuda = SimpleNamespace(
            is_available=lambda: True,
            memory_allocated=lambda: 20 * 1024 * MIB,
            memory_reserved=lambda: 22 * 1024 * MIB,
            max_memory_allocated=lambda: 30 * 1024 * MIB,
            mem_get_info=lambda: (10 * 1024 * MIB, 80 * 1024 * MIB),
        )
        with patch.dict(sys.modules, {"torch": SimpleNamespace(cuda=cuda)}):
            self.assertEqual(gpu_memory(), {
                "allocated_mib": 20480, "reserved_mib": 22528, "peak_allocated_mib": 30720,
                "device_used_mib": 71680, "device_total_mib": 81920,
            })


class AdapterStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_model_step_is_timed(self):
        adapter = omni_adapter()
        sess, _ = await adapter.open_session(
            {"session_id": "s", "model": "Realtime-Venus-Omni", "protocol_version": PROTOCOL_VERSION},
            asyncio.Event())
        adapter.receive(sess, {"type": "audio", "seq": 1, "ack": 0, "consumed": 0, "event_seq": 1,
                               "sample_rate_hz": 16000}, b"\0\0" * 16000)
        for _ in range(500):
            if any(h["type"] == "output" for h, _ in sess.outbox.after(0)):
                break
            await asyncio.sleep(0.01)
        stats = await adapter.stats()
        await adapter.close_session(sess, "test_end")
        self.assertEqual(stats["steps"]["count"], 1)
        self.assertIsNotNone(stats["steps"]["max_ms"])
        self.assertIn("gpu_memory", stats)
        self.assertIn("gpu_memory_after_load", stats)


class StatsEndpointTests(unittest.TestCase):
    def test_stats_require_the_token(self):
        stub = SimpleNamespace(_model_revision="r", _session=None, health=AsyncMock(return_value={}),
                               stats=AsyncMock(return_value={"steps": {"count": 3}}))
        with patch("demos.model.server._build_adapter", AsyncMock(return_value=stub)):
            with TestClient(create_app(_ServerConfig(model_path="/unused", token="secret"))) as client:
                self.assertEqual(client.get("/stats").status_code, 401)
                body = client.get("/stats", headers={"Authorization": "Bearer secret"}).json()
        self.assertEqual(body, {"steps": {"count": 3}})


if __name__ == "__main__":
    unittest.main()
