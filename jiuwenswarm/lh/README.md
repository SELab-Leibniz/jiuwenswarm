# `--lh`：long-horizon 控制器

本目录是 [long-horizon-controller](https://github.com/SELab-Leibniz/long-horizon-controller)
`965e3704d5cdcec8d2f838295ae6624d2e802c11` 中与 agent 无关的共享控制器核心，
只保留 JiuwenSwarm 原生 worker 插件和通用插件。Harbor/Terminal-Bench、Chrys、
Chrys ACP、C7 基线和固定公共 CA overlay 仍留在控制器仓库，不随本集成发布。

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

## MemGraph（默认关闭）

原 ContextGraph 集成已改名为 MemGraph，默认关闭：

- 控制器侧：只有同时提供 `--memgraph-command` 与 `--memgraph-receipt` 才启用只读
  语料查询；未配置时记录为 unavailable。
- 原生 worker 侧：只有设置 `JIUWENSWARM_MEMGRAPH_ENABLED=1` 并提供
  `MEMGRAPH_HTTP_URL`、`MEMGRAPH_EXPECTED_RECEIPT_SHA256`、`MEMGRAPH_EXPECTED_CORPUS`
  （以及调用 journal `MEMGRAPH_CALL_AUDIT_PATH`）时，才注册
  `jiuwenswarm.long_horizon.memgraph_mcp` 只读 MCP。

## 依赖

控制器需要 PACT Core `pact-core==0.2.0.dev0+bytes1.deadline1.turns1`，该 wheel
由控制器仓库的 `vendor/build_*_wheel.py` 构建，不提交到本仓库，也不加入默认安装；
只有使用 `--lh` 时才导入。
