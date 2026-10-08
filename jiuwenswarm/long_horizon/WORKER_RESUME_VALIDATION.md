# Jiuwen 接入最新 Core 与 Work 级 Worker 续接

## 交付范围

本分支基于 Jiuwen `6cfe9025af9cca10d6eab2d6ff470949745dd9b0`，接入 PR #22 合并后的 Core，加上本次 Worker 续接实现。精确来源和 wheel SHA256 见 [PACT_CORE_PIN.json](PACT_CORE_PIN.json)。代码仅在本地提交，未推送、未改主分支。

- campaign 更新为 `WorkRuntime + DefaultDriver`；原始题目入口仍使用 `BenchDriver`。
- 新增 `--worker-session resume`，默认 `new` 保持每轮新会话。
- 同一个 Work 的 P0、后续 Worker 及新的调用方 invocation 可以使用同一个原生 session。每次仍注入当前候选、约束、检查与 Review，不把旧对话当作当前事实。
- session 登记、原生 state 和历史落盘凭据持久化；退出后重新构造调用方也能恢复。
- 正常收尾的超时或取消可以接续；原始 trial 开始时间、截止时间与调用账本不重置。原始总预算耗尽后不会因此继续运行。
- Reviewer 始终新会话。无法恢复、历史缺失或被改动、执行配置/路径不符时明确报错，不自动新开 Worker 冒充续接。
- Core 同时提交候选和 session head；错误 session 的改动不会进入候选。

## 启用与保留材料

在原始题目 benchmark 的原有参数后添加 `--worker-session resume`。再次调用时，携带同一个 `--work-id`、当前 `--expected-revision`、相同模式和相同 trial 配置；输出使用新 invocation 目录。`run_campaign(..., worker_session="resume")` 支持单个 campaign 内各 Worker 轮次续接。

保留源项目的 `.pact/jiuwen-worker-sessions/`、Core Work、外部输出目录及其中的原生 state；重新启动时保持挂载路径不变。只复制 session ID 无法恢复。当前没有路径迁移能力。

原生进程退出前等待异步写入并 fsync 根 session 历史，生成 `session-checkpoint.json`。work 模式最多有 15 秒停止/清理宽限，真实耗时计入总预算，不是额外模型执行时间。强杀或清理未完成不能承诺恢复，必须明确处理。普通同次调用内的格式修正仍沿用首次调用的截止时间。

## 实际验证

环境：macOS arm64、Python 3.11.15，隔离 `.venv-pact`，未调用真实模型。

```sh
.venv-pact/bin/python -m pytest -o addopts='' -o log_cli=false -q \
  tests/long_horizon tests/unit_tests/process_cli \
  tests/unit_tests/test_session_history_atomic_write.py
```

结果：**1,130 passed、1 skipped、76 subtests passed，140.76 秒**。

测试覆盖真实 Git 与 Core API、受控原生进程、跨 invocation 恢复、SIGTERM 后历史落盘、超时后的下一调用、取消处理、错误 ID、修改后的历史、并发调用方拒绝、只读 Reviewer 与累计差异。跨 invocation 检查明确核对相同 session、原始 deadline 不变和累计调用次数增长。

Core 自身修复后的全量结果为 **1,357 passed、10 skipped**；strict mypy、两个零模型示例及 wheel 构建通过。

从 Core 提交归档构建的 wheel 与 Jiuwen 测试所用 wheel 完全相同；66 个 Python 文件分别与 Git 提交和已安装包逐字节核对。安装验证记录与原始日志存于本工作树 `scratch/worker-resume-validation/`，不进入提交。

## 尚未验证与兼容边界

- 仍使用 `fixed_command`。本次没有实现 Jiuwen host execution-evidence 导出，不能标为 `reviewer_evidence` 集成。
- 未运行付费模型 smoke、完整原生 SDK 或生产 Python 3.12 容器。协议测试通过不等于真实模型会正确利用历史，也不代表 benchmark 分数提升。
- 历史凭据证明根 session 对话持久化，不宣称保存了全部模型内部状态或任意子 Agent 的内存。
- Core 新记录为 work-state/v13、operation/v7、work-execution/v2，旧 Work 不自动迁移。
- 当前 Core 内置 Chrys/OpenCode/Fake 不声明原生续接能力；不能对其启用 resume。Jiuwen 是本次实现该能力的外部 adapter。

## 最终记录 SHA256

| 记录 | SHA256 |
| --- | --- |
| `jiuwen-pinned-final-suite.log` | `79ab3ab5af37dc2059b4e3851d12cfbf699f23e0a00dc928f14f6560645779b1` |
| `jiuwen-cross-invocation.log` | `cb38a1663b382e47dc2ccd47511f28359272bb9325eb2dd89295ffe2ce2df533` |
| `jiuwen-timeout-drain.log` | `43db26f998bc22eb28c0a45cbdbba6a1e973537f513c2dcd663ff6df3a77cac6` |
