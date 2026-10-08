# 固定 long-horizon 形态

本集成以 JiuwenSwarm `49c9f5b4c66d193e4168bfa9c82c2df025c773f0` 为基座，移植
Chrys 的长程工作流。入口始终执行 long-horizon，不经过路由分类器。迁移提交
是独立的被测版本，不能将其结果标为原始 JiuwenSwarm 提交的成绩。

参考工作流更新至 Chrys `0561cf0fa584fd56fdcd98c90cca6a5bc1890d95`。
流程为：冻结 S0 → P0 → S0 上的三路澄清调查、选择与可选代码定位 →
PACT 输入与公开检查 → 需要时进行 repair → PACT campaign。各个模型阶段和 PACT 的 Worker、Reviewer 都调用 JiuwenSwarm 原生机器接口；没有隐藏的
Chrys solver。`pact-core` 继续负责 campaign 状态、worktree、验证和接纳。

`clarification/` 保留了精确参考版本的 schema、提示、证据校验与快照逻辑，
实际模型适配改为 JiuwenSwarm。定位使用原生只读文件搜索，必须能核验 S0
文件、行范围和实际工具证据。初版关闭外部记忆、写回、CodeGraph 加速与
semantic index，配置和输出均记录这些选择。

另行命名的图实验可显式开启只读 ContextGraph MCP。图构建、embedding、
检索和 release 服务仍由 ContextGraph 仓库负责；本仓库只提供调用外部
`/health`、`/query`、`/evidence` 的 agent 工具。bench 固定来源 corpus、
receipt 和传输地址，启动前及每次调用均检查 release 身份。工具输出是
不可信的历史建议，不能替代当前源码和公开检查，不能写回图。

这些工具通过 JiuwenSwarm 原生 MCP 注册，仅供可修改代码的 P0、repair
及 PACT 角色自行调用。S0 只读调查及无工具结构化阶段不注册 MCP。
本模式没有 Chrys 的自动历史提示注入；这一差异属于被测实现，成绩必须
与关闭图的初版及 Chrys 的图实验分别报告。健康检查不能证明 agent 实际
查询了图，实际查询次数由不含请求/响应正文的配对诊断 journal 记录。

## 安装与执行

使用 Python 3.12.11 和仓库原 `uv.lock` 安装 JiuwenSwarm，再安装外部提供且
经 SHA-256 校验的 PACT wheel。依赖来源见 [PROVENANCE.md](PROVENANCE.md)。
正式评测由私有 [jiuwenswarm-bench](https://github.com/wzh4464/jiuwenswarm-bench)
构建内容寻址镜像、注入只含环境引用的模型配置并固定全部输入。

```bash
python -m jiuwenswarm.long_horizon \
  --workspace /workspace/project \
  --prompt-file /inputs/task.md \
  --output-dir /agent_out/long-horizon-suite \
  --worker-bin /opt/jiuwenswarm-bench/bin/jiuwenswarm-worker \
  --config-template /agent_out/jiuwenswarm/state/config/config.yaml \
  --timeout-seconds 172200
```

输出目录必须在项目外且每次新建。模型密钥只通过环境变量提供。bench 会
注入原始基座与实际 suite 提交的完整 SHA。工作流完成只说明执行链完整，
最终任务是否解决必须由独立 benchmark verifier 判定。

上述命令在 bench 构建的镜像内执行。worker launcher 先将原生 agent-core
日志指向该阶段的外部 state 目录，再进入原生机器接口，避免日志写入 S0。

## 预算与恢复

P0/repair 各有 6 小时基础预算，澄清及 PACT 输入各有 2 小时，PACT 单角色
有 3 小时；2 小时 reserve 纳入该阶段的累计预算，总时间仍受外层
上限约束。同会话格式纠正不重置截止时间。超时、输出预算耗尽、取消或
信号中断会保留真实会话、候选补丁、Git bundle 和阶段凭据。

PACT 每次调用默认最多 3 个 Worker 轮次，再进行必要的整体 Review；
这一上限不替代完整 Work 的时间预算。

输出耗尽根据原生流中的实际 usage/finish reason 事件识别，随后终止所属
进程组。这不保证复现 Chrys 内部在 validator/tool 之前暂停的精确时序；
此类未完成尝试不会进入独立判分。

阶段之间使用独立 Jiuwen session；阶段内有界格式纠正通过原生持久状态
恢复同一 session，每次使用新 request ID 和独立、不可变的调用凭据。
Manager 或 Planner 最多补交一次。失败、预算耗尽或中断后不再补交。
早期容器假模型探针观察到跨进程历史恢复，但不能仅凭相同 session ID 声称
完整历史已恢复。本次 opt-in Work 续接增加历史落盘确认，详见文末说明。
Reviewer 返回严格 JSON，由 adapter 保存为 PACT 的传输工件，
保持 reviewer 的只读工具边界。

公开检查先在 S0 上调查，再使用全新的无工具 session 合成；被拒绝的
工件只在该合成 session 内修正。无工具策略是本集成新增的显式原生扩展，
需确认实际模型请求不含工具定义；默认 Jiuwen Code 工具行为保持不变。
每条 AC 完成独立审查后才发布可复用检查点。

澄清 proposer 仍将调查和合成合并在原生 Code 调用内，全程允许 read/grep；
最多一次格式修正、一次证据补充。这与 Chrys 分开的 proposer 无工具合成
轮次不同。控制器还直接启动 PACT，省去 Chrys 的模型交接轮次；比较结果
需连同这些已记录的适配差异解释。

阶段快照保留源文件、已接受工件、真实调用凭据及配置身份，完成标记最后
写入。显式 P0 恢复必须验证包哈希、原始需求和干净的原 Git 基座，再跳过
初始 P0 模型调用，从澄清继续。恢复产物单独标记，不计作 fresh pass@1。
完整 workflow 和任意 PACT 角色的跨进程自动恢复仍未实现。

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

## 验证

```bash
.suite-venv/bin/python -m pytest -q \
  --confcutdir=tests/long_horizon -o addopts='' tests/long_horizon
```

这些测试使用原生协议的假进程、临时 Git 仓库和真实 PACT 控制平面，覆盖
只读权限、证据、预算/中断、补丁恢复、公开验证与流程接线；它们不产生
模型评测分数。容器安装、假模型工具探测、独立 grader 控制和真实模型
评测分别记录，不能互相替代。

更新后的独立入口为 `python -m jiuwenswarm.long_horizon.pact.long_horizon_benchmark`，
参考 Chrys `3f78185e298800b3e3f735f932a7ee2abb7bc25f`。它执行原始任务 → P0 →
S0 澄清 → PACT BenchDriver，不再强制先生成 Goal Contract、Initial Plan 与
公开 verifier。原生 Worker/Reviewer、澄清 side call 共享固定 trial 预算，
不经过路由。最终输出 `selection.json` 和 Core 封存候选；内部接纳和独立评测
分开记录。旧入口和旧测评工件保留，不能混入新版本成绩。

## 当前 Core 接口与 Worker 续接（2026-10-08）

新版本接入 PR #22 合并后的 `WorkRuntime`、`DefaultDriver` 和 `BenchDriver`。
旧的 `CampaignControlPlane`、Manager/Planner 调度接口不再用于 campaign 主路径；
公开检查覆盖完整原始合同，Reviewer 获得当前候选的累计差异。
本适配仍使用 `fixed_command` 验收；尚未实现 Jiuwen 的 host execution-evidence
导出，不能把它标成 `reviewer_evidence` 集成。

原始题目 benchmark 入口可选 `--worker-session resume`，默认 `new`。
P0 和后续 BenchDriver Worker 使用同一个 Work 的原生 session；Reviewer 始终新建。
显式 `--work-id` / `--expected-revision` 接续时保持相同模式与原始预算。
`run_campaign(..., worker_session="resume")` 则支持该 campaign 内各 Worker 轮次续接。

续接不仅传递 session ID：本地登记保存在源项目的 `.pact/jiuwen-worker-sessions/`，
原生 state 和调用工件保存在外部输出目录。原生进程收尾时等待异步历史写入，
对根 session 历史落盘并生成 `session-checkpoint.json`。下一调用核对历史摘要、
调用凭据、执行配置、角色和工作路径；使用新 request ID 及当前调用预算。
普通格式修正继续使用第一次调用的截止时间，两种模式分开。

超时/取消先停止并排空原生进程，work 模式最多允许 15 秒清理宽限，不增加模型
执行预算；实际清理耗时仍落入总预算账本。正常收尾留下历史凭据时可以接续。
强杀、缺失/变更的历史、旧 launcher 未生成凭据、错误 session ID、重复调用方
均明确失败，不自动另开会话。调用方重启要求保留并挂载原路径；当前未提供路径迁移。

Core 候选与原生记忆分开管理。原生会话里记得的事实仍须服从新一轮完整工作输入。
失败后的 Worker 改动可以按 Core 合同保存，但不能据此宣称验收通过。
新 Core 使用 work-state/v13，旧 Work 不自动迁移。安装时核对旁边的
`PACT_CORE_PIN.json`。当前开发系列为 `0.3.0.dev1`，提供给集成方的 wheel
使用 `0.3.0.dev1+g<完整提交 SHA>`；完整版本、源码 SHA 和 wheel 哈希一起固定。

本次本地验证使用真实 Git、Core 和受控原生进程/协议夹具，不调用付费模型。
这证明接续机制与故障边界，尚不证明真实模型的上下文利用效果或 benchmark 收益。

验证命令、结果和适用边界见 [WORKER_RESUME_VALIDATION.md](WORKER_RESUME_VALIDATION.md)。
