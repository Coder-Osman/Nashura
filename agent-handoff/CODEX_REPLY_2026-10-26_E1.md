# E0修正与E1阶段回执

## 最新续验回执：限定E1严格门通过

基于d49aab860ee8696808dcdf9118a30b55b3a2f5ff；本轮SHA以包含此回执的提交为准。
仅发布任务分支，不修改main。完整结果见
[FMOD与namespace续报](E1_FMOD_NAMESPACE_VALIDATION_2026-10-26.md)。

- 实际libdl resolver→FMOD六槽→Provider→保存manager传播链已动态验证。
- 24项真实引擎namespace/exists/优先级/fallback矩阵原版与替换一致。
- 最终3602份原生快照、28动作、终局、snapshot/restore/reset全字节或响应一致。
- libg9槽核验；正常生命周期平台AAsset事件0，Provider19474操作、失败0、live0。
- 全量9829资源/849482445bytes对照一致；同一Provider构建；990个update文件未变。
- `verify_e1_gate.py`真实附件CLI退出0，17项检查通过；20项真实证据负向突变全部拒绝。
- 专项pytest96通过、主工程211通过、公开入口完成，均exit0；工程与原生证据分开。
- 独立实例回收exit0，原adb身份不变；失败、损坏诊断及未覆盖项保留。

范围为冷启动至终局、战斗销毁及最终reset的配置观测边界。自然音频播放、
OS进程teardown、未配置间接调用及E0两个短stub仍未覆盖，不宣称穷尽生命周期。
Java VM和配置JNI保留，未进入E2、训练或10倍性能实验。

以下为旧阶段回执，其BLOCKED结果保留历史含义。

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
