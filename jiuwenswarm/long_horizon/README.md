# Jiuwen 长程工作流原生适配器

`jiuwenswarm-process --lh` 用 Jiuwen 的原生 worker 执行模型和工具调用。`jiuwenswarm.lh` 调度工作流，[PACT Core WorkRuntime](../pact_core/) 保存 Work、候选、公开检查、审查与交付状态。本目录提供两种工作流共用的冻结快照、澄清、原生角色和证据适配器。

## 安装与执行

```bash
uv sync --frozen --no-dev --no-editable
uv run --no-sync jiuwenswarm-process --lh --workflow suite --task /inputs/task.md --cwd /workspace/project
uv run --no-sync jiuwenswarm-process --lh --workflow benchmark --task /inputs/task.md --cwd /workspace/project
```

默认的 `suite` 从 S0 和 P0 生成 Goal Contract、Initial Plan 和 P1，再进入审查与修复。`benchmark` 以原始任务创建 Work，在 P0 和 S0 澄清后进入 BenchDriver。两者都使用仓库内的 WorkRuntime 源码。独立测评结果由外部 verifier 生成。

旧命令 `python -m jiuwenswarm.long_horizon` 现在输出迁移说明。启动新 Work 时把 `--workspace` 换成 `--cwd`、`--prompt-file` 换成 `--task`、`--output-dir` 换成 `--output`。指定输出目录时，选择 Git 工作区外的新目录。模型凭据通过原生配置的环境变量引用提供。[完整安装与参数说明](../lh/README.md)

## 原生阶段与证据

公开检查先在 S0 上调查，再使用全新的无工具 session 合成；被拒绝的
工件只在该合成 session 内修正。无工具策略是本集成新增的显式原生扩展，
需确认实际模型请求不含工具定义；默认 Jiuwen Code 工具行为保持不变。
每条 AC 完成独立审查后才发布可复用检查点。

原生 Deep/Code 及无工具合成可显式配置
`execution_guard.model_transport_retry: {enabled: true, backoff_seconds: [3, 7, 15]}`。
该策略默认关闭，只处理异常链中的 `httpx.ReadError`；使用固定 SDK 的
模型调用 rail，在失败的消息提交和工具分发之前重试当前调用。已完成的
工具和会话历史不重放，失败的流片段保留为观察记录而不提交到模型历史。
每次调用共享 SDK 的重试计数，配置最多五个、每个不超过 60 秒的退避。
重试仍受原阶段截止时间限制，取消、认证拒绝和其他异常不走这条策略。
`chat.model_transport_retry` 记录失败调用序号、预算和已观察片段数，不记录
模型文本、请求正文或凭据；原始错误记录仍保留。启用此策略属于新的
被测配置，需要冻结新 scaffold，不能将其结果覆盖到旧版本尝试。

MemGraph（原 ContextGraph 集成）默认关闭，只有设置 `JIUWENSWARM_MEMGRAPH_ENABLED=1`
并提供 `MEMGRAPH_HTTP_URL`、`MEMGRAPH_EXPECTED_RECEIPT_SHA256`、`MEMGRAPH_EXPECTED_CORPUS`
时才开启只读 MemGraph MCP。图构建、embedding、
检索和 release 服务仍由外部图服务负责；本仓库只提供调用外部
`/health`、`/query`、`/evidence` 的 agent 工具。bench 固定来源 corpus、
receipt 和传输地址，启动前及每次调用均检查 release 身份。工具输出是
不可信的历史建议，不能替代当前源码和公开检查，不能写回图。

这些工具通过 JiuwenSwarm 原生 MCP 注册，仅供可修改代码的 P0、repair
及 PACT 角色自行调用。S0 只读调查及无工具结构化阶段不注册 MCP。
本模式没有 Chrys 的自动历史提示注入；这一差异属于被测实现，成绩必须
与关闭图的初版及 Chrys 的图实验分别报告。健康检查不能证明 agent 实际
查询了图，实际查询次数由不含请求/响应正文的配对诊断 journal 记录。

## 读取归档的阶段检查点

离线工具不调用模型；在本集成的 Python 环境中执行：

```bash
python -m jiuwenswarm.long_horizon.workflow_checkpoint verify /path/to/checkpoint.json
python -m jiuwenswarm.long_horizon.workflow_checkpoint restore /path/to/checkpoint.json \
  --destination /new/empty/source
python -m jiuwenswarm.long_horizon.workflow_checkpoint export-p0 /path/to/long-horizon-suite \
  --destination /new/p0-package
```

`restore` 仅恢复源文件供检查。正式恢复运行由 bench 固定 package SHA-256
和原 attempt 身份，不能把任意修改过的工作目录作为已恢复 P0。

## 公开源码写入边界

专用 `pact.long_horizon_benchmark` 入口启用 `linux-dac/v2` 写入保护。
该模式要求固定 Linux 求解镜像中的 root 控制器和 `setpriv`；不支持的平台
在模型调用前失败。普通 `JiuwenWorker` 默认不启用此模式。

控制器只从原始公开源码的 Git 跟踪文件名建立测试清单：目录名为
`test`、`tests`、`__tests__`、`testdata`、`fixtures`、`__fixtures__`，或文件名
含 `.test.`、`.test-`、`.spec.`、`_test.`、`_tests.` 或以 `test_` 开头。
清单及其哈希记入 workspace policy；不读取独立 verifier 的补丁或排除路径。
已有公开测试保持只读。由这些公开文件名识别出的测试和夹具目录整棵树
保持只读，包括新文件和新子目录；源码及树外新建 `checks/` 检查仍可写。
该目录规则不读取隐藏排除路径，也不禁止树外新增回归检查。

可修改代码的原生进程及其子进程使用独立 UID/GID，清除补充组和 capabilities，
并禁止获取新权限。控制器持有目录及 Git 元数据，文件工具、shell、解释器
和格式化器共同受操作系统权限约束。`.pact`、`.pact-io` 的 ASCII 大小写
变体及 `.chrys`、`.semantic-search` 由控制器占用，避免原生工具污染 Core
控制目录。只读和无工具阶段沿用原有策略。

每次原生调用退出后，控制器先终止残留的该 UID 进程，再校验已有测试内容
和公开测试目录的条目、链接及目录身份，
移除自身的空目录占位文件并恢复权限。无法确认进程停止时保留保护并报错。
每次调用的 `native-write-guard.json` 保存完成或失败凭据。候选补丁仍由固定
Core 规范导出，不过滤、回滚或重写模型修改。使用新边界的尝试必须冻结新
suite/scaffold 身份；旧尝试和原成绩保持不变，修复重跑不能计作原始 pass@1。

启用图工具时，已建立的外部 MemGraph 调用日志临时交给原生 UID 追加，
阶段退出后恢复控制器所有权；不改写日志字节、查询结果、健康门槛或固定图身份。
