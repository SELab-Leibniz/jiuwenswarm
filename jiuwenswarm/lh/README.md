# `--lh`：long-horizon 控制器

本目录是 [long-horizon-controller](https://github.com/SELab-Leibniz/long-horizon-controller)
`bdf12e3db614becd77084a978f54368eb3e2c77c` 中与 agent 无关的共享控制器核心，
只保留 JiuwenSwarm 原生 worker 插件和通用插件。Harbor/Terminal-Bench、Chrys、
Chrys ACP、C7 基线和固定公共 CA overlay 仍留在控制器仓库，不随本集成发布。
候选归档、公开检查回执和交付使用的通用辅助函数随核心一起迁入；
来源与本地适配记录在 `SOURCE.json`。

suite 支持以下流程：

- 按当前 Work、输入与来源绑定的规划断点，以及独立 criterion 的并发复审和恢复。
- 隔离的可执行 Reviewer、检查方法诊断、审查准入与修复调度。
- 交付预留时间、检查生命周期、候选归档，以及基于完整需求的提前验收审计。
- 公开参考程序的多检查点比较；参考不可用、候选差异与可信回执分别记录。
- 原生事件的有界保留、内存压力证据、只读阶段的状态与日志隔离，以及 shell 进程终止保护。

使用 `--work-id` 与 `--expected-revision` 可以续跑已保存的原生 suite Work。
如果当前候选有符合条件的公开检查失败，控制器会把用例、检查点和复现方法交给
后续实际运行的 Worker、Planner 和独立 Reviewer。各角色沿用原 Goal Contract
和验收项 ID。新 Work 从自己的证据开始。

共享恢复逻辑支持由调用方确认旧执行器已停止并已切换会话的情况。重试前会校验
已封存的 P0 和中断的 P1 操作，并恢复原截止时间与连续停滞记录。持久会话切换由上游
external driver 管理。普通原生续跑遇到未封存的 P1 时会报告中断，供调用方检查。

## 入口

不带 `--lh` 时，`jiuwenswarm` 与 `jiuwenswarm-process` 的行为完全不变。带上
`--lh` 时，两个入口都在原有参数解析之前转到本控制器：

```bash
jiuwenswarm --lh "修复 issue #123" --cwd /path/to/repo
jiuwenswarm-process --lh --task task.md --cwd /path/to/repo --verify "pytest -q"
python -m jiuwenswarm.lh --task task.md --workdir /path/to/repo --output /tmp/lh-out
```

### 与 `--run-json` 组合（Beacon `JIUWENSWARM_EXTRA_ARGS`）

```bash
jiuwenswarm-process --run-json /tmp/jiuwenswarm_run.json --lh [控制器参数...]
```

- run JSON 提供题目（`input`）、工作区（`workspace.cwd`）和 `timeout_seconds`（作为控制器总预算，
  可用 `--budget-seconds` 覆盖）；`--lh` 之后的参数配置控制器，如 `--verify`、`--control-profile`。
  不能再给 prompt、`--task`、`--cwd`；未知参数、`mode` 非 `agent.code.normal`、带
  `session_id`/`model`/`output_schema`/`host_tools` 时，输出 `failed` 的 `result` 记录并以 2 退出。
- 控制器在工作区的私有副本里运行，`.pact` 账本和角色 worktree 不进入原仓库；结束后只把导出的
  候选补丁 `git apply` 回原工作区，由调用方照常采集 diff。
- 标准输出仍是机器协议 JSONL：启动时一条 `lh.enabled` 事件（启用证据，含 workflow、预算、
  控制器输出目录），结束时一条 `lh.finished` 事件（stop_reason、应用的文件），最后一条
  `result`，其 `usage` 为全部原生 worker 调用的 `usage.json` 之和。
- 退出码：0 已应用候选；4 无可导出候选；3 控制器内部失败；1 未发布 selection；5 补丁应用失败。

默认工作流为 `suite`：S0 → P0 → S0 上澄清/定位 → Goal Contract / Initial Plan →
P1 → 审查与修复循环 → 验收与交付，由 PACT Core 管理公开检查与接纳。各阶段
模型调用通过 `jiuwenswarm-process --run-json -` 进入原生机器接口
（`jiuwenswarm.long_horizon.worker.JiuwenWorker`）。

| 参数 | 默认值 |
| --- | --- |
| `--cwd` / `--workdir` | 当前目录（必须是 git 仓库） |
| `--output` | `$TMPDIR/jiuwenswarm-lh/<task-id>-<trial-id>`，必须在仓库外且为新目录 |
| `--worker-bin` | 与当前 Python 同目录的 `jiuwenswarm-process`，否则取 `PATH` |
| `--config-template` | 当前激活的 `config.yaml`；必须不含明文密钥（用环境变量引用） |
| `--budget-seconds` / `--timeout` | suite 48 小时，benchmark 1 小时 |

两种模式默认按原截止时间运行，实施轮数和角色调用总数均无默认上限。
`--max-stalled-turns N`（兼容拼写 `--max-turns N`）只限制连续无效的实施轮次。
例如，原有公开检查从失败转为通过，或独立审查确认剩余缺口减少，会把停滞计数清零，
并重新开放诊断和验收审查。相同的失败重复 N 轮后，控制器以
`implementation_stalled` 停止派发 Worker。换树、重复读取同一回执和恢复会话本身都不会补充额度。
累计调用、原截止时间以及调用方显式设置的 `--max-role-calls` 总开销上限保存在原 Work 账本里。

`--finish-reserve-seconds` / `--cutoff-at-unix` 绑定交付时间边界；
`--review-diagnosis-limit` / `--review-acceptance-limit` 设置审查熔断上限；
`--clarification-max-rounds` 限制无人值守下连续未解的问题轮次。待解决问题减少或修复通过检查会清零该计数，澄清时间仍受 `--clarification-budget-ratio` 约束。
`--early-audit-max-rounds` 限制早期验收后连续无效的覆盖修复轮次。新的进展会恢复审查和修复额度，替换已验收候选仍需通过公开检查和新一轮验收。
无人值守时默认按 suite 策略处理未决问题，`--allow-user-interaction` 保留人工答复等待。
Linux root 环境下可通过 `--reviewer-uid` / `--reviewer-gid` 配置专用 Reviewer 身份。

## MemGraph（默认关闭）

原 ContextGraph 集成已改名为 MemGraph，默认关闭：

- 控制器侧：只有同时提供 `--memgraph-command` 与 `--memgraph-receipt` 才启用只读
  语料查询；未配置时记录为 unavailable。
- 原生 worker 侧：只有设置 `JIUWENSWARM_MEMGRAPH_ENABLED=1` 并提供
  `MEMGRAPH_HTTP_URL`、`MEMGRAPH_EXPECTED_RECEIPT_SHA256`、`MEMGRAPH_EXPECTED_CORPUS`
  （以及调用 journal `MEMGRAPH_CALL_AUDIT_PATH`）时，才注册
  `jiuwenswarm.long_horizon.memgraph_mcp` 只读 MCP。

## 从 PR 安装

在本分支根目录使用 Python 3.11–3.13。WorkSwarm 随仓库提供 [PACT Core WorkRuntime 源码](../pact_core/)。默认的 `suite` 和原始任务 `benchmark` 都导入这份源码。快照过滤器需要的 `pathspec` 已纳入默认运行依赖；安装时无需 extra。`lh` extra 名称保留为兼容别名。完整审计范围与条件可选依赖见 [运行依赖检查](DEPENDENCIES.md)。

```bash
uv sync --frozen --no-dev --no-editable
uv run --no-sync jiuwenswarm-process --lh --help
uv run --no-sync python -I -m jiuwenswarm.lh.initialization_smoke --check-native-imports
```

初始化 smoke 使用临时 Git 仓库和非空 `.gitignore`，通过真实控制器、WorkRuntime 与原生快照实现完成 S0，在 P0 前停止，并禁止模型 worker 执行。它不需要 pytest 或开发依赖。可用 `--output /path/to/fresh-directory` 保留检查报告和 `selection.json`。该检查覆盖初始化，不代表模型执行、完整工作流或外部评测通过。

`--check-native-imports` 另在隔离子进程中用包内完整配置执行原生预检：按 Beacon seed 规则准备独立的数据目录，调用真实 `JiuwenWorker.run()`，分别验证可写、只读和禁用工具的角色配置隔离及请求 schema。检查在原生启动边界硬停止，不启动模型进程；环境变量名称保持原样。随后检查原生日志／状态目录和 Process CLI runtime 导入。全程禁止网络访问，保留配置快照、请求和 JSON 诊断；任一步失败或超时都会让整个 smoke 失败。

CI 在全新 Linux/Python 3.11 环境中按上述默认生产安装方式安装，再从源码目录之外、禁用网络的容器运行 smoke，避免开发依赖或源码路径掩盖安装缺漏。

默认配置的 `browser.decision.api_key_env` 填写环境变量名。OpenRouter 使用 `OPENROUTER_API_KEY`，TypeSafe 使用 `TYPESAFE_API_KEY`，共用主模型密钥时可填 `API_KEY`。密钥值由运行环境提供。

离线部署若使用 `--no-deps` 手动配置依赖，可用下列命令从仓库安装 `pathspec` 并核验 SHA-256。正常默认安装不需要此附加步骤。

```bash
python -m pip install -r jiuwenswarm/lh/requirements.txt
```

## 从单个 WorkSwarm wheel 安装

`uv build` 生成的 WorkSwarm wheel 和源码发行包均包含 WorkRuntime 的 Python 文件、角色配置和仪表盘资源。如果已有 JiuwenSwarm 基础依赖，下面的命令可用单个 WorkSwarm wheel 完成离线安装。解包步骤为 pip 提供内附的 `pathspec`。

```bash
python -m zipfile -e workswarm-0.2.5b1-py3-none-any.whl unpacked
python -m pip install -r unpacked/jiuwenswarm/lh/requirements.txt
python -m pip install --no-index --no-deps --force-reinstall ./workswarm-0.2.5b1-py3-none-any.whl
jiuwenswarm-process --lh --help
```

从旧的 `python -m jiuwenswarm.long_horizon` 迁移时，用 `jiuwenswarm-process --lh --workflow suite --task task.md --cwd /path/to/repo` 启动新 Work。运行原始任务流程时将 `suite` 改为 `benchmark`。[Core 版本、来源和源码校验值](SOURCE.json)
