"""Opt-in, bounded recovery of native model stream read failures.

The pinned SDK's model-call rail retries before an assistant message is committed
or its tools are dispatched. It retains the live session and completed tools;
it never repeats an invocation, PACT role or benchmark attempt.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import math
from typing import Any


logger = logging.getLogger(__name__)
POLICY = "native.model-read-retry.v1"


def transport_retry_options(config: Mapping[str, Any]) -> tuple[float, ...] | None:
    guard = config.get("execution_guard", {})
    if not isinstance(guard, Mapping):
        raise ValueError("execution_guard must be a mapping")
    policy = guard.get("model_transport_retry", {})
    if not isinstance(policy, Mapping) or set(policy) - {"enabled", "backoff_seconds"}:
        raise ValueError("invalid model_transport_retry policy")
    enabled = policy.get("enabled", False)
    if type(enabled) is not bool:
        raise ValueError("model_transport_retry.enabled must be boolean")
    if not enabled:
        if set(policy) - {"enabled"}:
            raise ValueError("disabled model_transport_retry must not configure retries")
        return None
    delays = policy.get("backoff_seconds", [3, 7, 15])
    if not isinstance(delays, (list, tuple)) or not 1 <= len(delays) <= 5:
        raise ValueError("model_transport_retry requires one to five delays")
    if any(isinstance(delay, bool) or not isinstance(delay, (int, float))
           or not math.isfinite(delay) or not 0 <= delay <= 60 for delay in delays):
        raise ValueError("model_transport_retry delays must be finite seconds from 0 to 60")
    return tuple(float(delay) for delay in delays)


def _read_failure(error: BaseException | None) -> bool:
    import httpx

    chain = []
    while error is not None and all(error is not item for item in chain):
        chain.append(error)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    # Never turn an HTTP rejection or cancellation into a transport retry.
    if any(not isinstance(item, Exception) or isinstance(item, httpx.HTTPStatusError) for item in chain):
        return False
    return any(isinstance(item, httpx.ReadError) for item in chain)


def build_transport_retry_rail(config: Mapping[str, Any]) -> Any | None:
    delays = transport_retry_options(config)
    if delays is None:
        return None
    from openjiuwen.core.session.stream import OutputSchema
    from openjiuwen.harness.rails.base import DeepAgentRail

    class NativeModelReadRetryRail(DeepAgentRail):
        # The SDK timeout/repetition rail uses 70. This hook runs afterwards and
        # shares the SDK's attempt counter instead of opening another budget.
        priority = 60

        async def on_model_exception(self, ctx: Any) -> None:
            if not _read_failure(ctx.exception):
                return
            attempt = ctx.retry_attempt
            if attempt >= len(delays):
                logger.warning("Native model read retry exhausted: policy=%s attempts=%s",
                               POLICY, attempt + 1)
                return
            delay = delays[attempt]
            # Failed chunks are observations only; the SDK rebuilds its local
            # accumulator on re-entry. Do not alter conversation/tool history.
            ctx.inputs.response = None
            ctx.request_retry(delay_seconds=delay)
            payload = {"policy": POLICY, "failed_attempt": attempt + 1,
                       "retry_limit": len(delays), "delay_seconds": delay,
                       "cause": "httpx.ReadError",
                       "discarded_stream_chunks": ctx.extra.get("_stream_chunks_emitted", 0)}
            logger.warning("Native model read retry requested: %s", payload)
            if ctx.session is not None:
                await ctx.session.write_stream(OutputSchema(
                    type="chat.model_transport_retry", index=0, payload=payload,
                ))

    return NativeModelReadRetryRail()
