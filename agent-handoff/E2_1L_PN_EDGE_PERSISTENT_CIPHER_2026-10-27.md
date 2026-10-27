# E2.1-L：PN 异常语义与持久解密状态

## 结论与边界

本轮从 `a492c85990dee9e83463cbd61ad5698d48355b95` 继续，只更新任务分支
`codex/headless-engine-plan-20261025`。已取得真实 PN 原生入口的缺失、空值、
损坏密文及回退观测，并用独立持久 EVP 解密器复现原 Cipher 失败后的状态变化。
限定诊断验证 exit 0；不是自然入口替换验收，E2.1 strict 仍 BLOCKED。

九个独立原版进程各完成 3,602 个原生快照、28 动作、终局及 snapshot/restore/reset，
轨迹 9,120,440 bytes 全字节一致。异常矩阵均在完整轨迹和 reset **之后**执行。
因此本轮没有证明异常之后再运行完整对局仍一致，也没有新增自然 PN 替换证据。

保留 Java 身份、原 store 构造、原 Cipher、SharedPreferences、backup、VM 及 Android。
32 键历史替换范围未扩大，46 个排除键移除数仍为 0、可移除性 unknown。
未执行 no-VM、Android-free、训练、多机或 10× 性能实验。

## 代码与验证器

- `e2_1_pn_edge_matrix.py`：完整轨迹后通过真实原生 PN read/write 入口运行有界矩阵，
  保存实际 fixture、返回、JNI 异常状态、真实 commit 事件、XML 条目变化及恢复回执。
- `E21PreferenceObserver.java`：新增有界 raw pair fixture、原 Cipher.b.doFinal 调用及
  runtime 信息观测；只在明确启用的实验命令中使用，不替换游戏 Cipher。
- `e2_1_pn_stream.c/.h`：两个 store 各有独立 EVP 解密上下文。失败时保留实际 EVP
  状态；成功 final 后才按原 key/IV 重新初始化。错误不输出临时明文。
- `e2_1_pn_stream_probe.js`：受限显式差分入口，不接管自然 PN 桥；每 store 最多
  16 次调用，记录真实错误码、输出和 close/call counts，关闭后擦除实验密钥材料。
- `run_e2_observed.py`：冻结实际源码、显式配置并校验 stream 实验前提；异常先保存
  primary 错误，再进行 finally 证据收尾，保留两种失败。
- `verify_e2_1l_edges.py`：验证九 run 身份、完整轨迹、资源、原输入、复编、矩阵
  语义、原始 XML 非 PN 条目、持久状态差分与独立进程清理。输出严格脱敏摘要；
  native 差分不一致会失败，不以“记录 mismatch”代替验收。

## 实际 PN 语义

19 项主矩阵涵盖双缺失、单 store 缺失、plain 旧键、密文冲突、空 raw/空明文密文、
非法 Base64、非整块、坏 padding、首选损坏、空写入和覆盖写入。另以独立进程改变
执行顺序，区分 fixture 本身与之前失败遗留的 Cipher 状态。

| 原入口观测 | 实际结果 | 范围 |
|---|---|---|
| 双密文键缺失 | 空串，零 commit | 两 store 的真实缺失 |
| 首选缺失、旧 store 有效 | 返回旧值，首选真实回填一次 | 新鲜有效解密状态 |
| 首选有效、旧 store 缺失 | 返回首选值，零 commit | 新鲜有效解密状态 |
| 只有 plain `PN_POPUP` | 忽略，原 plain 条目保留 | fixture 的 legacy 名称不代表支持迁移 |
| 两密文值冲突 | 首选值胜出，零 commit | 本次固定输入 |
| 新鲜首选非法 Base64、旧值有效 | 首选清理及回填共两次 commit，返回旧值 | 独立 malformed-first 进程 |
| 坏块或 padding 后的读取 | 后续有效密文也可能失败并被移除 | 依赖先前 Cipher 状态 |
| 写空值且条目存在 | 两 store 各真实 remove/commit | 双缺失时零 commit |
| 覆盖损坏条目 | 可先清理再写入，每 store 两次 commit | 主矩阵实际顺序 |

正常完成的原桥 read/write 前后 JNI ExceptionCheck 均为 false。这不证明内部没有
Java 异常；显式调用原 Cipher 已捕获真实异常。write 是 void，不将返回寄存器解释
为成功状态。每行原生调用来源、线程、phase、commit 和 XML 证据留在私有附件。

## 关键发现：失败并非无状态

原 Cipher.b 的直接调用在三个独立进程中各做八次；其中两次同时做独立 EVP 差分：

| 顺序 | 原 Cipher | 持久 EVP |
|---|---|---|
| store 0：有效 → 3-byte 非整块 → 同有效 → 同有效 | 16 bytes → IllegalBlockSizeException → 同异常 → 同异常 | 成功 → -4 → -4 → -4 |
| store 1：有效 → 16-byte 坏 padding → 同有效 → 同有效 | 16 bytes → BadPaddingException → 32 bytes → 16 bytes | 成功 → -4 → 同 32 bytes → 同 16 bytes |

两次独立进程共 16 次 native 差分，返回/异常分类对应状态及全部输出字节一致。
不能以永久 poisoned 标记描述 padding 后状态，也不能在每次失败后无条件 reset。
本实现保留真实 EVP context；没有预制上述输出或统一返回空值。

native -4 尚未细分 Java 异常类型，因此异常类别等价仍 unknown。codec-runtime
没有取得 CipherSpi 内部实例字段；内部 native context 仍 opaque，不据此声称无状态。
密钥材料沿用 K 阶段已验证来源，本轮不声称重新独立证明提取过程。

## 完整轨迹、身份与构建

九 run：baseline、original-edges、original-malformed-first、original-padding-first、
original-short-state、original-padding-state、original-direct-cipher、
original-stream-diff-3、original-stream-diff-4；各用独立游戏 PID。

共同轨迹 SHA256：
`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`。
各 17 项非 status 控制回执一致，终局 tick 3,601；原九项输入和 990 update 身份保持。
九 libg 资源槽与六 FMOD 绑定保持，平台 AssetManager 事件、Provider failures、live
assets 均为 0。该覆盖不外推 JNI、配置或全部 Android 依赖。

Android ARM64 clang shared、javac --release 8、d8 三条复编命令均 exit 0，部署产物
与复编逐字节一致。自有 stream SO SHA 为
`79f9c2c845e9abccedf438be3ce5d44f7dedbaf67de4e4934ea3e80159a16e8a`，
自有 DEX SHA 为
`2d10924c1057cc4d748cb53319ababccb1deba5ed676062654a186a262f8c72e`。
这些自有产物未上传；原 APK、libg、libfmod、probe、Provider 未修改。

33,058 附件验证通过，私有 manifest SHA：
`402bbe9594b8ede7254c13c8a9883382868c799ff8f96d0302b7a16cb62cad48`。
公开只保存源码、报告和脱敏摘要；XML、身份、密钥、二进制和逐 tick 快照保留本机。
所有已完成 run 的所属游戏进程已按 PID/starttime 释放；本轮独占 AVD/adb 保留，
供下一项真实替换实验使用，历史 adb 保持。未声称实例已完全回收。

## 测试与失败保留

- 限定附件验证 exit 0：bounded_diagnostic_verified，strict_gate=BLOCKED。
- L 专项：42 passed / 7.12s / exit 0，包含真实九 run 附件和损坏负测。
- E2 回归首轮：1 failed / 348 passed / 309 skipped / exit 1。
  Java 释放 fixture 的 JSONObject 桩缺少新增 API；补齐桩声明，未改变实际 Android 代码。
- 修正后：349 passed / 309 skipped / 149 deselected / 12.03s / exit 0。
  私有附件缺失的测试明确 skip；集合有重叠，不累加为原生通过数。
- 保留两次 stream 实验失败：首次缺 derivation 开关；第二次错误导出名，且 finally
  缺 snapshot 覆盖 primary。原始两种错误仍保留，修正后第三、四次各 exit 0。

原 XML 恢复按条目语义比较，XML 序列化顺序可能改变；不写成 XML 原字节恢复。
这些运行耗时包含插桩，不是吞吐或 10× 基准。发布检查实际结果见同阶段 Codex 回执。

## 首个未解决依赖与下一步

首个缺口是将上述持久解密和清理/回退语义接入真实 PN 替换入口，随后在异常之后
再次完成原生对局、snapshot/restore/reset。当前 K 替换仍只接受 present-ciphertext-v1，
不会静默放宽其旧契约或套用旧证据。

新替换应使用独立版本，逐项对照原入口返回、commit、XML、JNI 异常与后续解密状态。
Java 身份、原 store/XML/backup、commit-false、并发及崩溃语义分别保留未完成项。
达到限定替换门槛后再推进身份/存储契约；整体 strict、no-VM 和 Android-free 单独验收。
