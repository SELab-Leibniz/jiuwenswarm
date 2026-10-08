# 0.3.0 开发系列接入验证（2026-10-08）

基于已有 Worker 续接实现，只升级包版本管理，不改变业务逻辑和验收模式。

- 源码基础版本：`0.3.0.dev1`；正式 `0.3.0` 尚未发布。
- Core 源码提交：`f47bd8d4cf22c5b3bcc3ec108938d062be3665cb`。
- Jiuwen 安装版本：`0.3.0.dev1+gf47bd8d4cf22c5b3bcc3ec108938d062be3665cb`。
- Wheel SHA256：`495b4ab4fe3d51280dfbb5476691ee19fc5efa6169e0a4084cfd94417b32e543`。
- 使用 Core `scripts/build_dev_wheel.py` 从干净提交归档构建；工作树不被改写，已有产物不覆盖。
- Python API、安装元数据、CLI 的完整版本一致；包内构建记录保存完整源提交。
- 66 个 Python 文件与源码和已安装文件核对通过。构建只替换版本赋值并增加构建身份 JSON；与前一轮已做全量测试的 wheel 相比，Python 文件仅 `__init__.py` 的版本赋值变化。

本次针对性验证：Core 构建边界与 CLI **28 passed**；Jiuwen PACT 与原始任务 benchmark 接入 **29 passed、14 subtests passed**。已有输出目录的重复构建请求被拒绝，旧 wheel 保留。

本次没有重跑完整 Core/Jiuwen 测试，也没有真实模型调用。前一轮完整测试的历史结论见 `WORKER_RESUME_VALIDATION.md`，不改写为本次新提交的完整测试结果。

完整选择见 `PACT_CORE_PIN.json`。原始日志和安装内容核对记录保存于本地 `scratch/worker-resume-validation/`。

| 日志 | SHA256 |
| --- | --- |
| `jiuwen-030-pin-tests.log` | `2ddd5cd96076d68233535de837a81cb4be7bbc1b166063ed8dcc38dfdc6546b5` |
| `pact-030-version-tests.log` | `4b69a89f64d898a54f78dc6fb12934505ec3052dc918857cd02e611400ab9d7f` |
| `pact-030-build.log` | `89e8d6a2468c750a3b1a26c2356c2718d0c110a5aaaa927655fc69e3dc36c589` |
| `pact-030-no-overwrite.log` | `e7669ebe469e7b2e594195f29c2a53ae09e584e005ddca8000fb97623f7f26a3` |
