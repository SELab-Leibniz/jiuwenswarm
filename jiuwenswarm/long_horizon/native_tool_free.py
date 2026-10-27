"""Explicit, opt-in native Code profile for tool-free synthesis.

The profile uses the pinned native DeepAgent, model, session, and event path.
It omits tool-producing capabilities at construction; it does not simulate
tool absence with permission denials or call a replacement model client.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


PROFILE = "code.no-tools.v1"


class NativeToolPolicyViolation(RuntimeError):
    """Fail closed if the no-tools construction or execution contract drifts."""

    def __init__(self, reason: str):
        super().__init__("JIUWENSWARM_TOOL_POLICY_VIOLATION: " + reason)


def code_tool_policy(config: Mapping[str, Any]) -> str:
    modes = config.get("modes") or {}
    code = modes.get("code") or {}
    policy = code.get("tool_policy", "configured")
    if policy not in ("configured", "none"):
        raise NativeToolPolicyViolation("unsupported Code tool policy")
    return policy


def assert_tool_free_agent(agent: Any) -> None:
    manager = getattr(agent, "ability_manager", None)
    if manager is None or not callable(getattr(manager, "list", None)):
        raise NativeToolPolicyViolation("native ability manager is unavailable")
    if manager.list():
        raise NativeToolPolicyViolation("native abilities were registered")
    config = getattr(agent, "deep_config", None)
    if config is not None and any(getattr(config, key, None) for key in
                                  ("tools", "mcps", "subagents", "sys_operation")):
        raise NativeToolPolicyViolation("native tool resources were configured")


async def _finish_policy_violation(ctx: Any, reason: str) -> None:
    """Use native termination control, since callback exceptions are swallowed.

    Pinned CallbackFramework.trigger logs ordinary callback exceptions and
    continues. ReAct checks request_force_finish immediately after a model call,
    before adding or dispatching its tool calls, and before decorated operations.
    Set that native signal before emitting the native error observation so even
    an event transport error cannot turn a policy failure into model/tool work.
    """
    from openjiuwen.core.session.stream import OutputSchema

    message = str(NativeToolPolicyViolation(reason))
    ctx.request_force_finish({"result_type": "error", "output": message})
    if ctx.session is not None:
        await ctx.session.write_stream(OutputSchema(
            type="error", index=0,
            payload={"error": message, "code": "TOOL_POLICY_VIOLATION"},
        ))


def build_tool_free_rail() -> Any:
    # Lazy imports keep deterministic profile validation independent of the
    # native runtime installation. Actual construction always uses native Rail.
    from openjiuwen.core.session.stream import OutputSchema
    from openjiuwen.harness.rails.base import DeepAgentRail

    class NativeToolFreeRail(DeepAgentRail):
        # Run assertions after the retained prompt/context/stream rails.
        priority = -1_000_000

        def init(self, agent: Any) -> None:
            assert_tool_free_agent(agent)

        async def before_model_call(self, ctx: Any) -> None:
            try:
                assert_tool_free_agent(ctx.agent)
            except NativeToolPolicyViolation:
                await _finish_policy_violation(ctx, "native tool resources or abilities were registered")
                return
            if getattr(ctx.inputs, "tools", None):
                await _finish_policy_violation(ctx, "model input contains tools")
                return
            if ctx.session is None:
                await _finish_policy_violation(ctx, "native event session is unavailable")
                return
            await ctx.session.write_stream(OutputSchema(
                type="chat.tool_policy", index=0,
                payload={"policy": "none", "registered_tools": [],
                         "verified": True, "profile": PROFILE},
            ))

        async def after_model_call(self, ctx: Any) -> None:
            response = getattr(ctx.inputs, "response", None)
            tool_calls = (response.get("tool_calls") if isinstance(response, Mapping)
                          else getattr(response, "tool_calls", None))
            if tool_calls:
                await _finish_policy_violation(ctx, "model returned a tool call")

        async def before_tool_call(self, ctx: Any) -> None:
            # Defense in depth: no tool is registered, and even a future
            # unexpected execution path must fail before invoking a tool.
            await _finish_policy_violation(ctx, "tool execution was attempted")

    return NativeToolFreeRail()


def build_tool_free_completion_rail() -> Any:
    """Keep native interaction lifecycle without its two standing goal tools.

    Pinned DeepAgent.start() adds TaskCompletionRail even when task loops are
    disabled. Supplying its subclass makes native recognize the lifecycle rail
    while preventing init() from registering get_current_goal/submit_goal_report.
    Normal-turn callbacks remain native; tool-free synthesis cannot run goals.
    """
    from openjiuwen.harness.rails.task_completion_rail import TaskCompletionRail

    class NativeToolFreeCompletionRail(TaskCompletionRail):
        def init(self, agent: Any) -> None:
            self.attachment_manager = getattr(agent, "prompt_attachment_manager", None)
            assert_tool_free_agent(agent)

        async def before_task_iteration(self, ctx: Any) -> None:
            if self._get_run_kind(ctx) == "goal":
                await _finish_policy_violation(ctx, "goals are disabled in tool-free synthesis")
                return
            await super().before_task_iteration(ctx)

    return NativeToolFreeCompletionRail()


def build_tool_free_rails(adapter: Any) -> list[Any]:
    """Retain native non-tool prompt, stream and context capabilities only."""
    rails = []
    for attribute, builder in (
        ("_runtime_prompt_rail", "_build_runtime_prompt_rail"),
        ("_response_prompt_rail", "_build_response_prompt_rail"),
        ("_stream_event_rail", "_build_stream_event_rail"),
        ("_security_rail", "_build_security_rail"),
        ("_context_processor_rail", "_build_context_processor_rail"),
    ):
        rail = getattr(adapter, builder)()
        if rail is None:
            raise NativeToolPolicyViolation("required native synthesis rail is unavailable")
        setattr(adapter, attribute, rail)
        rails.append(rail)
    rails.append(build_tool_free_completion_rail())
    rails.append(build_tool_free_rail())
    return rails
