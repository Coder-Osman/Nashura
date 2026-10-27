# E2.1-C 阶段回执

**真实提交返回、写失败及不同值交错的限定证据推进完成；G-E2.1 仍 BLOCKED。**

- 基线：`68d015c68068e7d2d67d0b80b1d2b1a88d4c3e9a`。
- 分支：`codex/headless-engine-plan-20261025`，只发布此任务分支。
- main/origin/main：`0012201830cfbfcbfeb3d9dccd91a855028da869`，保持原样。
- 本次 commit 为包含本回执的阶段提交，实际 SHA 及远端核验在最终交付消息提供。

## 实际交付

- 双原 SharedPreferences 的临时转发观测器，直接取得委托对象的 commit boolean。
- 原桥受控写失败和不同值双线程 helper；严格记录 void ABI、JNI 异常状态及线程身份边界。
- 独立 ART partial 解析、原始附件验证器、白名单公开摘要、工程测试及保留失败索引。
- 六个独立原生进程，各3602快照/9120440bytes、28动作、tick3601终局和restore/reset一致。
  当前observer源码通过完整轨迹非扰动验证；平台AssetManager0、Provider failures0/live0。
- 变化/空值/恢复写均两次真实commit(true)，同值与读取零commit。
- 写失败时两个原commit均false、原桥无pending JNI异常；内存新值可见，磁盘XML字节未变。
  权限恢复后变化写双commit true，最后恢复保存值及双store、原权限。
- 不同值双线程8写8读、6对真实ART root重叠；7次读自身值、1次读另一线程值。
  两store分别7/6次commit(true)，不证明双store原子性、线性化或一般线程安全。
- 46键仍原路由，可移除性unknown；PN未native替换。ART未知ID显式保留，旧严格解析器不变。

## 实际验证

| 命令/检查 | 实际结果 | exit |
|---|---|---|
| `tools/run_e2_observed.py` 六次独立运行 | 每次完整局与生命周期对照通过 | 0 |
| `tools/verify_e2_1c_evidence.py` | 22,039私有附件，bounded_evidence_verified | 0 |
| `tools/e2_1c_public_summary.py` | 重验真实附件后白名单脱敏导出 | 0 |
| 两个新增test文件，设置真实manifest | 50 passed / 14.39s | 0 |
| `tools/verify_e2_1_gate.py`，旧真实manifest | 严格门BLOCKED | 2 |
| 两个JS `node --check` | 语法检查通过 | 0 |
| Java/d8/NDK API30复编 | DEX/SO与实测字节完全一致 | 0 |
| `tools/run_main_engine_checks.py` | 211 passed / 40.30s | 0 |
| `tools/run_public_checks.py` | 主组794 passed/27 skipped，下列八组全部通过 | 0 |

公开后续八组：affordability 23 passed；selection-timeout 82 passed；position-guards 15 passed/3 skipped；
live-command 25 passed/4 skipped；dispatch-command 31 passed/3 skipped；reservation-mapping 31 passed/2 skipped；
command-coordinates 38 passed/3 skipped；collection-coordinator 65 passed。
这些工程集合有重叠，不相加，不替代真实原生语义验收或无VM门禁。

发布检查：`tools/check_public_snapshot.py` exit0，核验2,463个文件；
`gitleaks dir . --config .gitleaks.toml --redact` exit0，扫描约34.07MB、无泄露；
`git diff --check` exit0。新增文件还需在暂存后执行 `git diff --cached --check`，
其实际结果及提交/远端SHA由最终交付消息记录。

实际命令、构建身份、故障记录及覆盖界限详见
`E2_1C_PN_COMMIT_FAILURE_CONCURRENCY_2026-10-27.md`。
公开摘要只是索引；不随仓库上传APK、原生库、DEX/SO、配置值或原始私有审计。

## 未解决依赖与下一步

首项为PN native候选的持久化/写失败等价，以及Java引用退休；本轮仍保留原实现。
自然cold-start commit返回未覆盖，崩溃恢复/一般并发未知；46键尚无移除反事实。
下一轮分别补自然初始化观测、PN候选的独立语义矩阵、46键真实消费者，以及close/free和重复初始化ABI。
严格门继续fail-closed；未进入E2.2/no-VM、Android-free、训练、G0/G1或10×。

本轮独占引擎、Frida、AVD和专用adb已核验身份后回收；五个专用端口监听0，
原有5037 adb的PID/启动时间/命令保持一致，克隆磁盘及失败附件保留。
