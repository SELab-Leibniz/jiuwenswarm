# LH 运行依赖审计

审计基线：`69e22ba6a8dea671cae69b1643fb98416967a4b9`。本次修复将
`pathspec==1.1.1` 纳入项目默认依赖，使普通 `uv sync --frozen --no-dev`
也能运行含非空 `.gitignore` 的 LH 初始化。审计未发现第二个必须新增的
LH 默认 Python 依赖。

## 直接依赖

扫描范围为 `jiuwenswarm/lh`、`jiuwenswarm/long_horizon` 和内置
`jiuwenswarm/pact_core` 的全部 159 个 Python 文件，包含函数内延迟导入。
安装声明及锁定结果由根目录 [pyproject.toml](../../pyproject.toml) 和
[uv.lock](../../uv.lock) 管理。

| 导入名 | 发行包／默认声明 | 用途与审计结果 |
| --- | --- | --- |
| `pathspec` | `pathspec==1.1.1` | [GitignoreFilter](../long_horizon/clarification/compat.py#L167-L187) 在规则非空时延迟导入；S0 快照就会调用。基线仅在 `lh` extra 中声明，必须进入默认依赖。 |
| `yaml` | `pyyaml>=6.0,<7` | [worker.py](../long_horizon/worker.py#L31) 读取及隔离 Native 配置；已是默认依赖。 |
| `pydantic` | `pydantic>=2.0,<3.0` | [clarification/types.py](../long_horizon/clarification/types.py#L12)、model、service 和 checkpoint 数据模型；已是默认依赖。 |
| `openjiuwen` | 固定 Git revision 的 `openjiuwen` | [native_entry.py](../long_horizon/native_entry.py#L255-L256) 配置日志并启动 Native runtime，其他 rails 也使用它；已是默认依赖。 |
| `httpx` | `httpx[http2,socks]>=0.27,<1.0` | [native_transport_retry.py](../long_horizon/native_transport_retry.py#L43-L61) 的可选流读取重试；已是默认依赖。 |
| `mcp` | `mcp>=1.28.1,<2` | [memgraph_mcp.py](../long_horizon/memgraph_mcp.py#L18) 的可选 MemGraph 服务；已是默认依赖。 |

`pact_core` 使用包内 `jiuwenswarm.pact_core`，无需再安装独立 PACT wheel。
基线锁文件的默认依赖闭包不含 `pathspec`：另一个引用它的包是开发用途的
`mypy`，不能依靠开发环境顺带安装。默认安装的回归检查应排除开发／测试依赖。

## 条件可选依赖

沿 `lh.cli`、`lh.run_json`、`long_horizon.native_entry` 和
`channels.process_cli.machine` 的本仓库导入继续追踪，得到 860 个模块的
保守静态可达并集。这包含条件分支，并不表示每次 LH 都执行全部模块。
将第三方导入映射到基线默认锁文件后，其余未覆盖项均有明确可选边界：

| 导入名 | 为何无需加入 LH 默认依赖 |
| --- | --- |
| `jiuwenswarm_sdk` | [worker.py](../long_horizon/worker.py#L1663-L1715) 捕获 SDK 导入失败，使用本地严格 JSON／chunk decoder；无 SDK 是支持的路径。 |
| `claude_agent_sdk` | [config_set_handlers.py](../common/config_panel/config_set_handlers.py#L792-L799) 仅在查询 Claude CLI 参考版本时导入，失败返回空字符串。 |
| `tree_sitter_language_pack` | [code_graph_setup.py](../server/runtime/agent_adapter/code_graph_setup.py#L26-L67) 是显式启用 Code Graph 后的可选能力，未安装时返回不可用并回退；不是 S0 快照依赖。 |
| `zmq` | [remote_member_bootstrap.py](../agents/harness/team/remote_member_bootstrap.py#L745-L763) 用于分布式 teammate，导入失败有处理；本地 Native Code 路径不要求该能力。 |

这次修改不扩大这些可选功能的安装范围。静态依赖闭包按所有 marker 分支保守
展开；具体平台的实际安装仍须验证，不能用闭包分析代替运行检查。

## 动态加载与打包资源

[NativeArtifacts／NativeClarification](plugins/native.py#L64-L79)、
[native_checkpoint](plugins/native_checkpoint.py#L359) 和
[checkpoint_guard](checkpoint_guard.py#L659-L673) 的动态导入目标均在本包内。
审计也查看了 reviewer privilege 和 worker 中生成的子进程 Python 源码，
这些片段仅使用标准库。任意外部 adapter、配置指定的工具／MCP 命令及其依赖
不属于可穷举的默认 Native 路径。

[worker.py](../long_horizon/worker.py#L2707-L2751) 的可写角色执行配置的
`jiuwenswarm-process --run-json -`；只读角色执行同一环境的 Python
`-m jiuwenswarm.long_horizon.native_entry --run-json -`，随后进入普通
Process CLI。因此只检查 controller import，无法覆盖子进程的运行环境。

需要随 wheel 保留的资源包括：

- `jiuwenswarm/resources/config.yaml`：只读角色配置合并时实际读取。
- `jiuwenswarm/channels/process_cli/protocol/schema.json`：Native 协议校验。
- `jiuwenswarm/pact_core/adapters/profiles/*.yaml`、`runtime/dashboard_assets/*`。
- LH 的 `SOURCE.json`、`requirements.txt`、内置 pathspec wheel，以及
  `long_horizon/requirements.lock`。

现有 `package-data` 声明覆盖上述资源；最终 wheel 仍应检查内容并从安装目录
读取资源。内置 wheel 文件本身不会使普通安装自动安装它，默认依赖声明才是
此次修复的关键。

## 验证边界

项目声明支持 Python 3.11–3.13；这三个目录的 159 个 Python 文件通过了
Python 3.11 语法模式解析。此检查不证明全部标准库 API 或平台行为兼容。
LH 的 POSIX 权限／只读 reviewer 路径还依赖目标系统，应在 Linux 环境验证。

无模型初始化 smoke 应使用临时 Git 仓库和非空 `.gitignore`，执行真实
WorkRuntime、S0 导出与快照，断言没有 `internal_failure`，且已经到达模型
调用前的可识别边界；模型角色用明确的本地测试替身阻止调用。只运行
`--lh --help` 或 `import jiuwenswarm.lh.cli` 不会触发这个延迟依赖。

分别验证默认源码安装和普通 wheel 安装，并检查导出资源，可发现开发环境
或源码目录掩盖的依赖问题。此类 smoke 证明初始化与打包，不证明真实模型
调用、服务端配置、整题执行或评测成绩；实际执行结果记录在 [VALIDATION.md](VALIDATION.md)。
