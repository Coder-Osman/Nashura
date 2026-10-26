# E0修正与E1阶段回执

基线提交：`74769af2c95fb095ae970a3b8c89934206805aea`。
本阶段提交身份以包含本回执的Git提交为准；仅提交任务分支
`codex/headless-engine-plan-20261025`，不修改main。

## 已完成

- 修正GameStateSetup阶段归因，区分同线程scope与并发时间重叠；两个失败hook
  保留未覆盖、次数未知，不把零计数解释为依赖不存在。
- 实现只读APK资源provider及libg九槽重定向，保留Java VM、配置JNI与原初始化。
- 固定原生完整局3602份快照全字节一致，28动作、snapshot/restore、reset通过。
- 9829真实APK资源、849482445bytes对照零差异，描述符和缺失请求另行核验。

## 实际命令与结果

- 专项pytest：111 passed，exit0；精确命令见E1报告第5节。
- `python tools/run_main_engine_checks.py`：211 passed，exit0。
- `python tools/run_public_checks.py`：主组794 passed/27 skipped，八组专项完成，exit0。
- `python tools/verify_e1_gate.py --comparison … --counts … --redirect … --semantics … --output …`：
  exit2，BLOCKED；真实私有输入及独占输出保留，省略号不是可直接执行的路径。
- 独立实例身份核验及回收：exit0；所属监听残留0，原adb身份不变，证据保留。
- 公开清单校验：2384项通过，exit0；gitleaks无泄漏，exit0；差异空白检查exit0。

## 未解决依赖与下一步

E1整体未通过：真实平台残留1次由FMOD发起的AAssetManager_fromJava；
APK/update namespace优先级及fallback的一般边界尚未验证。下一项仅调查
FMOD资源绑定来源和引擎资源分派，构造冲突/缺失矩阵后重新完成独立原生对照。
E1严格通过后再提出E2；本轮未启动E2、正式训练或吞吐放量。

完整结果及失败证据索引：E1_RESOURCE_PROVIDER_2026-10-26.md；
脱敏聚合：publication/e0-e1-resource-evidence.json。
