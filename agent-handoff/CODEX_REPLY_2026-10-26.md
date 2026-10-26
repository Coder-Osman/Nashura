# Codex回执：边界诊断与本机E0（2026-10-26）

## 任务身份

- 基线 `b3947d8720fa0645f78acf15f72546992da2d4b6`；分支 `codex/headless-engine-plan-20261025`。
- 提交SHA以本报告所属提交为准，不合并或推送main。
- 原始libg、冻结任务、历史部署和训练准入保持原样；正式训练未启动。

## 阶段A：REA/Ghidra边界时间盒

隔离数据库预声明41个callee后，两入口越界P-code从9/2308降为4/2023；
显式数据库尾部CALL_RETURN流覆盖后0/0。219/175条汇编及32/28条直接BL不变，源ELF SHA不变。
这是分析器控制流假设验证，不证明ABI/函数语义。真实Ghidra合成集成测试4 passed、18.36s、exit0。
命令及限制见 `NATIVE_BATTLE_DEPENDENCY_CLOSURE_CONTINUATION_2026-10-26.md`。

## 阶段B：E0动态观测

用户授权后克隆已有ARM64 AVD，在本机独立adb5039/e0-only使用匹配libg、合法历史离线包装APK、reference probe和精确990资源。
无插桩1局、成功插桩2局，均原生tick3601终局、28动作、队列空。
两次插桩各3602份完整原生serialized快照与基线逐字节相同；restore分支及reset通过。

v3实测VM9,991、JNI43,434、AAsset19,474个去重事件，并非平台依赖为零。
配置桥及命令间接调用捕获；2个短stub挂钩失败，queue/world/manager_218、线程阶段归因仍有缺口。
所有依赖的删除后状态影响未知。本轮仅固定轨迹不变性通过，不是覆盖穷尽。
首次spawn超时与v2启用额外回溯后SIGSEGV失败记录保留。

完整身份、阶段计数、来源、命令和限制见 `E0_DYNAMIC_DEPENDENCY_COUNTS_2026-10-26.md`。

## 验证命令与结果

```sh
"$PY" -m pytest -q -p no:cacheprovider tools/tests/test_e0_snapshot_verifier.py \
  tools/tests/test_rea_evidence.py tools/tests/test_headless_native.py \
  tools/tests/test_headless_trace_verifier.py
# Python3.13.5，exit0，69 passed in 0.17s
node --check tools/e0_dependency_observer.js
# exit0
```

严格快照比较exit0，3602份/9,120,440bytes一致。
本阶段完整公开入口exit0：主组794 passed/27 skipped；其余组23、82、15/3、25/4、31/3、31/2、38/3、65；
主工程211 passed（33.07s）。集合不累加，跳过不替代原生覆盖。
旧系统Python曾22 failed/47 passed，既有hashlib.file_digest版本要求导致；现代解释器复验通过。
公开清单、gitleaks及diff检查在发布前执行，实际结果见提交说明。

## 回收与后续门

已核验身份并停止本轮游戏、Frida、AVD、转发及adb5039；原adb5037保留，克隆和失败证据留存。
E1/E2未执行；无VM/框架完整对局和10倍吞吐均未验证，`VM_FRAMEWORK_FREE_INCONCLUSIVE`。
首个未解决依赖为资源provider完整Android语义及冷启动配置JNI契约，另有间接调用/线程归因缺口。
下一项为保留VM、只替换资源provider的独立E1，本轮不自动扩大实验。
