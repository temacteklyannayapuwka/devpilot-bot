import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from codex_limits import LimitsUnavailable, read_limits


class LimitsProtocolTests(unittest.IsolatedAsyncioTestCase):
    def process(self, messages):
        process = Mock()
        process.returncode = None
        process.stdin.drain = AsyncMock()
        process.stdout.readline = AsyncMock(side_effect=[
            (json.dumps(message) + "\n").encode() for message in messages
        ])
        process.wait = AsyncMock(return_value=0)
        return process

    async def test_handshake_and_limit_response_skip_notifications(self):
        process = self.process([
            {"id": 1, "result": {}},
            {"method": "account/updated", "params": {}},
            {"id": 2, "result": {"rateLimits": {"primary": None}}},
        ])
        with patch("codex_limits.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            result = await read_limits()
        sent = [json.loads(call.args[0]) for call in process.stdin.write.call_args_list]
        self.assertEqual([item["method"] for item in sent], [
            "initialize", "initialized", "account/rateLimits/read"
        ])
        self.assertEqual(result, {"rateLimits": {"primary": None}})
        process.terminate.assert_called_once()

    async def test_server_error_is_redacted_and_process_closed(self):
        process = self.process([
            {"id": 1, "result": {}},
            {"id": 2, "error": {"message": "private credentials"}},
        ])
        with patch("codex_limits.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaises(LimitsUnavailable) as raised:
                await read_limits()
        self.assertNotIn("private credentials", str(raised.exception))
        process.terminate.assert_called_once()

    async def test_hung_server_times_out_and_is_closed(self):
        process = self.process([])

        async def hang():
            await asyncio.Event().wait()

        process.stdout.readline = hang
        real_wait_for = asyncio.wait_for

        async def short_timeout(awaitable, timeout):
            return await real_wait_for(awaitable, min(timeout, 0.02))

        with patch("codex_limits.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with patch("codex_limits.asyncio.wait_for", short_timeout):
                with self.assertRaises(LimitsUnavailable):
                    await read_limits()
        process.terminate.assert_called_once()
