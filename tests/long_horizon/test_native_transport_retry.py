"""Transport recovery classification, finite budgets and native policy wiring."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx

from jiuwenswarm.long_horizon.native_transport_retry import (
    _read_failure, build_transport_retry_rail, transport_retry_options,
)


def _wrapped(error):
    wrapper = RuntimeError("native model call failed")
    wrapper.__cause__ = error
    return wrapper


class TransportRetryTests(unittest.IsolatedAsyncioTestCase):
    def rail(self, delays=(0, 0)):
        base = ModuleType("openjiuwen.harness.rails.base")
        base.DeepAgentRail = type("DeepAgentRail", (), {})
        stream = ModuleType("openjiuwen.core.session.stream")
        stream.OutputSchema = SimpleNamespace
        with patch.dict(sys.modules, {base.__name__: base, stream.__name__: stream}):
            return build_transport_retry_rail({"execution_guard": {
                "model_transport_retry": {"enabled": True, "backoff_seconds": list(delays)},
            }})

    def test_disabled_policy_needs_no_native_sdk_and_unknown_config_fails(self):
        self.assertIsNone(build_transport_retry_rail({}))
        self.assertEqual(transport_retry_options({"execution_guard": {
            "model_transport_retry": {"enabled": True}}}), (3.0, 7.0, 15.0))
        for policy in ({"enabled": 1}, {"enabled": True, "backoff_seconds": []},
                       {"enabled": True, "backoff_seconds": [True]},
                       {"enabled": True, "backoff_seconds": [float("inf")]},
                       {"enabled": True, "backoff_seconds": [61]},
                       {"enabled": True, "backoff_seconds": [0] * 6},
                       {"enabled": False, "backoff_seconds": [0]},
                       {"enabled": True, "retry_whole_task": True}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                transport_retry_options({"execution_guard": {"model_transport_retry": policy}})

    def test_only_typed_read_error_causes_are_retryable(self):
        self.assertTrue(_read_failure(_wrapped(httpx.ReadError(""))))
        self.assertFalse(_read_failure(RuntimeError("ReadError")))
        self.assertFalse(_read_failure(httpx.ReadTimeout("timeout")))
        self.assertFalse(_read_failure(_wrapped(asyncio.CancelledError())))
        rejected = httpx.HTTPStatusError("unauthorized", request=httpx.Request("POST", "https://example.invalid"),
                                         response=httpx.Response(401))
        rejected.__cause__ = httpx.ReadError("")
        self.assertFalse(_read_failure(_wrapped(rejected)))
        loop = RuntimeError("cycle")
        loop.__cause__ = loop
        self.assertFalse(_read_failure(loop))

    async def test_same_wire_budget_stops_and_does_not_modify_completed_history(self):
        rail = self.rail((3, 7))
        messages = [object()]
        ctx = SimpleNamespace(exception=_wrapped(httpx.ReadError("secret must not appear")),
                              retry_attempt=0, inputs=SimpleNamespace(response=None, messages=messages),
                              extra={"_stream_chunks_emitted": 4}, request_retry=Mock(),
                              session=SimpleNamespace(write_stream=AsyncMock()))
        for attempt in range(4):
            ctx.retry_attempt = attempt
            await rail.on_model_exception(ctx)
        self.assertEqual(ctx.request_retry.call_count, 2)
        self.assertEqual([call.kwargs["delay_seconds"] for call in ctx.request_retry.call_args_list], [3, 7])
        self.assertIs(ctx.inputs.messages, messages)
        self.assertEqual(ctx.session.write_stream.await_count, 2)
        event = ctx.session.write_stream.await_args_list[0].args[0]
        self.assertEqual(event.payload["discarded_stream_chunks"], 4)
        self.assertNotIn("secret", repr(event.payload))
        # A fresh model call gets the SDK's fresh counter, with the same rail.
        ctx.retry_attempt = 0
        await rail.on_model_exception(ctx)
        self.assertEqual(ctx.request_retry.call_count, 3)

    async def test_observation_failure_does_not_remove_native_retry_request(self):
        rail = self.rail()
        ctx = SimpleNamespace(exception=httpx.ReadError(""), retry_attempt=0,
                              inputs=SimpleNamespace(response=None), extra={}, request_retry=Mock(),
                              session=SimpleNamespace(write_stream=AsyncMock(side_effect=OSError())))
        with self.assertRaises(OSError):
            await rail.on_model_exception(ctx)
        ctx.request_retry.assert_called_once_with(delay_seconds=0)
