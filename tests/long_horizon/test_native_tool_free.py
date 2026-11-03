"""Tool-free construction and fail-closed checks, without model calls.

Wire transport and actual agent-core construction are separately exercised by
the frozen-image fake SSE probe. These tests do not substitute for that probe.
"""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from jiuwenswarm.long_horizon.native_tool_free import (
    NativeToolPolicyViolation, assert_tool_free_agent, build_tool_free_rail,
    build_tool_free_completion_rail,
    build_tool_free_rails, code_tool_policy,
)


ROOT = Path(__file__).resolve().parents[2]


def _native_stubs():
    base = ModuleType("openjiuwen.harness.rails.base")
    base.DeepAgentRail = type("DeepAgentRail", (), {})
    stream = ModuleType("openjiuwen.core.session.stream")
    stream.OutputSchema = SimpleNamespace
    completion = ModuleType("openjiuwen.harness.rails.task_completion_rail")

    class TaskCompletionRail:
        def __init__(self):
            self.normal_calls = 0

        def init(self, agent):
            raise AssertionError("native standing goal tools must not be registered")

        @staticmethod
        def _get_run_kind(ctx):
            return ctx.run_kind

        async def before_task_iteration(self, ctx):
            self.normal_calls += 1

    completion.TaskCompletionRail = TaskCompletionRail
    return {base.__name__: base, stream.__name__: stream, completion.__name__: completion}


def _agent(**kwargs):
    return SimpleNamespace(ability_manager=SimpleNamespace(list=lambda: []),
                           deep_config=SimpleNamespace(**kwargs))


def _method(path, class_name, name, globals_):
    tree = ast.parse(path.read_text())
    owner = next(item for item in tree.body if isinstance(item, ast.ClassDef) and item.name == class_name)
    function = next(item for item in owner.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name)
    node = ast.Module(body=[function], type_ignores=[])
    # Native dependencies are unavailable in the deterministic suite env.
    # Execute the actual method body with injected collaborators, not a copy.
    exec(compile(ast.fix_missing_locations(node), str(path), "exec", flags=__import__("__future__").annotations.compiler_flag), globals_)
    return globals_[name]


class ToolFreeProfileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        native_modules = patch.dict(sys.modules, _native_stubs())
        native_modules.start()
        self.addCleanup(native_modules.stop)

    def test_explicit_opt_in_and_unknown_policy_fails(self):
        self.assertEqual(code_tool_policy({}), "configured")
        self.assertEqual(code_tool_policy({"modes": {"code": {"tool_policy": "none"}}}), "none")
        with self.assertRaises(NativeToolPolicyViolation):
            code_tool_policy({"modes": {"code": {"tool_policy": "deny"}}})

    def test_registration_or_resource_presence_is_not_treated_as_deny(self):
        assert_tool_free_agent(_agent())
        for key, value in (("tools", [object()]), ("mcps", [object()]),
                           ("subagents", [object()]), ("sys_operation", object())):
            with self.subTest(key=key), self.assertRaises(NativeToolPolicyViolation):
                assert_tool_free_agent(_agent(**{key: value}))
        agent = _agent()
        agent.ability_manager.list = lambda: [SimpleNamespace(name="read_file")]
        with self.assertRaises(NativeToolPolicyViolation):
            assert_tool_free_agent(agent)

    async def test_proof_is_emitted_for_each_native_model_call(self):
        with patch.dict(sys.modules, _native_stubs()):
            rail = build_tool_free_rail()
        ctx = SimpleNamespace(agent=_agent(), inputs=SimpleNamespace(tools=None),
                              session=SimpleNamespace(write_stream=AsyncMock()))
        await rail.before_model_call(ctx)
        ctx.inputs.tools = []
        await rail.before_model_call(ctx)
        self.assertEqual(ctx.session.write_stream.await_count, 2)
        for call in ctx.session.write_stream.await_args_list:
            event = call.args[0]
            self.assertEqual(event.type, "chat.tool_policy")
            self.assertEqual(event.payload, {"policy": "none", "registered_tools": [],
                                            "verified": True, "profile": "code.no-tools.v1"})

    async def test_dynamic_tool_registration_fails_before_model(self):
        with patch.dict(sys.modules, _native_stubs()):
            rail = build_tool_free_rail()
        ctx = SimpleNamespace(agent=_agent(), inputs=SimpleNamespace(tools=[object()]),
                              session=SimpleNamespace(write_stream=AsyncMock()),
                              request_force_finish=Mock())
        await rail.before_model_call(ctx)
        self.assertEqual(ctx.request_force_finish.call_args.args[0]["result_type"], "error")
        self.assertEqual(ctx.session.write_stream.await_args.args[0].type, "error")
        ctx.inputs.tools = None
        ctx.agent.ability_manager.list = lambda: [object()]
        await rail.before_model_call(ctx)
        self.assertEqual(ctx.request_force_finish.call_count, 2)
        self.assertEqual(ctx.session.write_stream.await_args.args[0].type, "error")

    async def test_malicious_tool_call_fails_without_execution(self):
        with patch.dict(sys.modules, _native_stubs()):
            rail = build_tool_free_rail()
        for response in ({"tool_calls": [{"name": "bash"}]},
                         SimpleNamespace(tool_calls=[SimpleNamespace(name="write_file")])):
            with self.subTest(response=response):
                ctx = SimpleNamespace(inputs=SimpleNamespace(response=response),
                                      session=SimpleNamespace(write_stream=AsyncMock()),
                                      request_force_finish=Mock())
                await rail.after_model_call(ctx)
                result = ctx.request_force_finish.call_args.args[0]
                self.assertEqual(result["result_type"], "error")
                self.assertIn("JIUWENSWARM_TOOL_POLICY_VIOLATION", result["output"])
                self.assertEqual(ctx.session.write_stream.await_args.args[0].type, "error")
        await rail.after_model_call(SimpleNamespace(inputs=SimpleNamespace(response=SimpleNamespace(content="{}", tool_calls=[]))))
        ctx = SimpleNamespace(session=None, request_force_finish=Mock())
        await rail.before_tool_call(ctx)
        self.assertEqual(ctx.request_force_finish.call_args.args[0]["result_type"], "error")

    async def test_callback_error_swallowing_cannot_remove_native_stop_signal(self):
        rail = build_tool_free_rail()
        ctx = SimpleNamespace(inputs=SimpleNamespace(response={"tool_calls": [{"name": "bash"}]}),
                              session=SimpleNamespace(write_stream=AsyncMock(side_effect=OSError("closed stream"))),
                              request_force_finish=Mock())
        # Regression for pinned CallbackFramework.trigger: it catches ordinary
        # callback exceptions. The native stop signal must precede even a failed
        # observation write and survive that exception swallowing.
        try:
            await rail.after_model_call(ctx)
        except Exception:
            pass
        result = ctx.request_force_finish.call_args.args[0]
        self.assertEqual(result["result_type"], "error")
        self.assertIn("JIUWENSWARM_TOOL_POLICY_VIOLATION", result["output"])

    def test_only_non_tool_native_rails_are_built(self):
        adapter = SimpleNamespace()
        names = ("runtime_prompt", "response_prompt", "stream_event", "security", "context_processor")
        for name in names:
            setattr(adapter, f"_build_{name}_rail", Mock(return_value=object()))
        with patch.dict(sys.modules, _native_stubs()):
            rails = build_tool_free_rails(adapter)
        self.assertEqual(len(rails), 7)
        for name in names:
            builder = getattr(adapter, f"_build_{name}_rail")
            builder.assert_called_once_with()
            self.assertIs(getattr(adapter, f"_{name}_rail"), builder.return_value)

    async def test_native_interaction_completion_rail_never_registers_goal_tools(self):
        stubs = _native_stubs()
        with patch.dict(sys.modules, stubs):
            rail = build_tool_free_completion_rail()
        self.assertIsInstance(rail, stubs["openjiuwen.harness.rails.task_completion_rail"].TaskCompletionRail)
        agent = _agent()
        agent.prompt_attachment_manager = object()
        rail.init(agent)
        # Native start binds goal_manager and calls init again. Both remain
        # tool-free; there is no unregister-after-initialization workaround.
        agent.goal_manager = object()
        rail.init(agent)
        self.assertIs(rail.attachment_manager, agent.prompt_attachment_manager)
        await rail.before_task_iteration(SimpleNamespace(run_kind="user"))
        self.assertEqual(rail.normal_calls, 1)
        ctx = SimpleNamespace(run_kind="goal", session=None, request_force_finish=Mock())
        await rail.before_task_iteration(ctx)
        self.assertEqual(ctx.request_force_finish.call_args.args[0]["result_type"], "error")
        self.assertEqual(rail.normal_calls, 1)

    async def test_toolfree_config_reload_cannot_enable_tools(self):
        path = ROOT / "jiuwenswarm/server/runtime/agent_adapter/interface_code.py"
        method = _method(path, "JiuwenSwarmCodeAdapter", "reload_agent_config", {
            "code_tool_policy": code_tool_policy,
            "NativeToolPolicyViolation": NativeToolPolicyViolation,
            "assert_tool_free_agent": assert_tool_free_agent,
        })
        adapter = SimpleNamespace(_native_tool_policy="none", _instance=_agent())
        await method(adapter, {"modes": {"code": {"tool_policy": "none"}}})
        with self.assertRaises(NativeToolPolicyViolation):
            await method(adapter, {"modes": {"code": {"tool_policy": "configured"}}})

    async def test_toolfree_extension_mounts_fail(self):
        path = ROOT / "jiuwenswarm/server/runtime/agent_adapter/interface_code.py"
        method = _method(path, "JiuwenSwarmCodeAdapter", "_ensure_chat_extensions", {
            "NativeToolPolicyViolation": NativeToolPolicyViolation,
            "assert_tool_free_agent": assert_tool_free_agent,
        })
        adapter = SimpleNamespace(_native_tool_policy="none", _instance=_agent())
        self.assertIsNone(await method(adapter, SimpleNamespace(params={})))
        for key in ("agent_template_name", "plugin_names", "mcp"):
            with self.subTest(key=key), self.assertRaises(NativeToolPolicyViolation):
                await method(adapter, SimpleNamespace(params={key: ["untrusted-extension"]}))

    def test_native_proof_survives_stream_parser_without_text_projection(self):
        path = ROOT / "jiuwenswarm/server/runtime/agent_adapter/interface_code.py"
        delegate = SimpleNamespace(_parse_stream_chunk=Mock(return_value={"event_type": "chat.delta", "content": "normal"}))
        method = _method(path, "JiuwenSwarmCodeAdapter", "_parse_stream_chunk", {
            "NativeToolPolicyViolation": NativeToolPolicyViolation,
            "JiuWenSwarmDeepAdapter": delegate,
        })
        proof = {"policy": "none", "registered_tools": [], "verified": True,
                 "profile": "code.no-tools.v1"}
        parsed = method(SimpleNamespace(type="chat.tool_policy", payload=proof))
        self.assertEqual(parsed, {**proof, "event_type": "chat.tool_policy"})
        self.assertNotIn("content", parsed)
        delegate._parse_stream_chunk.assert_not_called()
        normal = SimpleNamespace(type="llm_output", payload={"content": "normal"})
        self.assertEqual(method(normal), {"event_type": "chat.delta", "content": "normal"})
        delegate._parse_stream_chunk.assert_called_once_with(normal, _has_streamed_content=False,
                                                            _stage="", _parent_session_id=None)

    def test_native_spec_omits_tool_providers_and_sysoperation(self):
        path = ROOT / "jiuwenswarm/agents/harness/code/spec.py"
        tree = ast.parse(path.read_text())
        function = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "convert_code_config_to_deep_agent_spec")
        scope = {"deepcopy": deepcopy, "code_tool_policy": code_tool_policy,
                 "register_code_spec_providers": lambda: None,
                 "_optional_max_iterations": lambda x: x,
                 "_model_spec": lambda model: model,
                 "_sys_operation_spec": lambda op, card: (op, card),
                 "get_skill_evolution_enabled": lambda _: False,
                 "is_subagent_runtime_enabled": lambda _: True,
                 "get_progressive_tool_enabled": lambda _: True,
                 "CODE_RAIL_BUNDLE": "rail", "CODE_TOOL_BUNDLE": "tool",
                 "CODE_SUBAGENT_BUNDLE": "subagent"}
        for name in ("CodeBuildContext", "DeepAgentSpec", "RailSpec", "BuiltinToolSpec",
                     "SubAgentSpec", "AgentCard", "WorkspaceSpec", "ProgressiveToolSpec"):
            scope[name] = SimpleNamespace
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(path), "exec",
                     flags=__import__("__future__").annotations.compiler_flag), scope)
        for policy in ("configured", "none"):
            with self.subTest(policy=policy):
                spec, _ = scope[function.name](adapter=SimpleNamespace(_tool_owner_id=lambda: "owner"),
                    config_base={"modes": {"code": {"tool_policy": policy, "tools": ["web_free_search"], "rails": ["SkillUseRail"]}}},
                    react_config={"enable_task_loop": True}, model="original-model",
                    card=SimpleNamespace(id="agent"), system_prompt="original-system-prompt",
                    workspace_root="/state", project_dir="/workspace", sys_operation=None,
                    sys_operation_card=None, language="en", context_engine_config="original-context",
                    kv_cache_affinity_config="original-affinity", enable_read_image_multimodal=False,
                    completion_timeout=600)
                self.assertEqual(spec.model, "original-model")
                self.assertEqual(spec.context_engine_config, "original-context")
                if policy == "none":
                    self.assertEqual(spec.tools, [])
                    self.assertEqual(spec.subagents, [])
                    self.assertIsNone(spec.sys_operation)
                    self.assertFalse(spec.enable_sys_operation)
                    self.assertFalse(spec.enable_task_loop)
                    self.assertFalse(spec.enable_subagent_runtime)
                    self.assertFalse(spec.enable_tool_resilience_rail)
                    self.assertFalse(spec.progressive_tool.enabled)
                else:
                    self.assertEqual(len(spec.tools), 1)
                    self.assertEqual(len(spec.subagents), 1)
                    self.assertTrue(spec.enable_task_loop)
                    self.assertTrue(spec.enable_subagent_runtime)


if __name__ == "__main__":
    unittest.main()
