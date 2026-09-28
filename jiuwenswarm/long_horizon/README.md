# 固定 long-horizon 形态

本集成以 JiuwenSwarm `49c9f5b4c66d193e4168bfa9c82c2df025c773f0` 为基座，移植
Chrys 的长程工作流。入口始终执行 long-horizon，不经过路由分类器。迁移提交
是独立的被测版本，不能将其结果标为原始 JiuwenSwarm 提交的成绩。

流程为：冻结 S0 → P0 → S0 上的三路澄清调查、选择及代码定位 → repair →
PACT 输入与公开检查 → PACT campaign。各个模型阶段和 PACT 的 Manager、
Planner、Worker、Reviewer 都调用 JiuwenSwarm 原生机器接口；没有隐藏的
Chrys solver。`pact-core` 继续负责 campaign 状态、worktree、验证和接纳。

`clarification/` 保留了精确参考版本的 schema、提示、证据校验与快照逻辑，
实际模型适配改为 JiuwenSwarm。定位使用原生只读文件搜索，必须能核验 S0
文件、行范围和实际工具证据。初版关闭外部记忆、写回、CodeGraph 加速与
semantic index，配置和输出均记录这些选择。

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
有 3 小时；2 小时 reserve 合并进同一次存活的原生调用，总时间仍受外层
上限约束。不会通过重新启动阶段伪装续跑。超时、输出预算耗尽、取消或
信号中断会保留真实会话、候选补丁、Git bundle 和阶段凭据。

PACT 每个 mission 最多 3 个 Worker/Reviewer 回合，与固定 ChrysPact profile
一致；这一上限不替代完整 campaign 的时间预算。

输出耗尽根据原生流中的实际 usage/finish reason 事件识别，随后终止所属
进程组。这不保证复现 Chrys 内部在 validator/tool 之前暂停的精确时序；
此类未完成尝试不会进入独立判分。

这一版的各阶段是独立 Jiuwen session，尚不自动跨进程继续。Manager 或
Planner 未交出有效 JSON 时保留协议错误；不会新建会话冒充 Chrys 原本的
同会话补交。Reviewer 返回严格 JSON，由 adapter 保存为 PACT 的传输工件，
保持 reviewer 的只读工具边界。

## 验证

```bash
.suite-venv/bin/python -m pytest -q \
  --confcutdir=tests/long_horizon -o addopts='' tests/long_horizon
```

这些测试使用原生协议的假进程、临时 Git 仓库和真实 PACT 控制平面，覆盖
只读权限、证据、预算/中断、补丁恢复、公开验证与流程接线；它们不产生
模型评测分数。容器安装、假模型工具探测、独立 grader 控制和真实模型
评测分别记录，不能互相替代。
