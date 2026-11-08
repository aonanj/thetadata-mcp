import asyncio
from pathlib import Path
import os
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import runtime


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_limit_does_not_start_another_worker(self):
        slots = asyncio.Semaphore(1)
        await slots.acquire()
        with patch.object(runtime, "slots", slots), patch.object(runtime, "run_worker", new_callable=AsyncMock) as worker:
            result = await runtime.execute("chain", {})
            self.assertEqual(result["error"]["code"], "BUSY")
            worker.assert_not_awaited()

    async def test_capacity_is_released_after_worker_failure(self):
        slots = asyncio.Semaphore(1)
        with patch.object(runtime, "slots", slots), patch.object(runtime, "run_worker", new_callable=AsyncMock, side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                await runtime.execute("chain", {})
        self.assertFalse(slots.locked())

    async def test_worker_environment_excludes_client_token(self):
        proc = Mock(returncode=0)
        proc.communicate = AsyncMock(return_value=(b'{"ok": true}', None))
        with patch.dict(os.environ, {"MCP_API_KEY": "fake-service-token", "THETADATA_API_KEY": "fake-provider-key",
                                     "MCP_OAUTH_ACCESS_TOKEN": "fake-oauth-token"}), \
             patch("runtime.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc) as spawn:
            result = await runtime.run_worker("chain", {})
        self.assertTrue(result["ok"])
        env = spawn.call_args.kwargs["env"]
        self.assertNotIn("MCP_API_KEY", env)
        self.assertNotIn("MCP_OAUTH_ACCESS_TOKEN", env)
        self.assertEqual(env["THETADATA_API_KEY"], "fake-provider-key")

    async def test_cancellation_terminates_worker(self):
        started = asyncio.Event()

        async def blocked(*args):
            started.set()
            await asyncio.Event().wait()

        proc = Mock(returncode=None)
        proc.communicate = AsyncMock(side_effect=blocked)
        proc.wait = AsyncMock()
        with patch("runtime.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc):
            task = asyncio.create_task(runtime.run_worker("chain", {}))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        proc.kill.assert_called_once()
        proc.wait.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
