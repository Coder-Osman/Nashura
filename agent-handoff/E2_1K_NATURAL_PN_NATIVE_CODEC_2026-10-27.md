# E2.1-K：自然冷启动 PN 原生派生与 AES

## 结论与边界

已把 J 阶段在完整轨迹后显式调用的原生派生/AES，接到原游戏自然冷启动的
PN_POPUP 调用点。两个独立替换进程通过固定完整局、逐 tick 原生字节、原 Java
读回和跨进程持久值对照。限定验证 exit 0；E2.1 整体仍 BLOCKED。

保留原初始化顺序、Java App 身份获取、两个原 store 的构造、SharedPreferences
XML writer、backup 路径、Java VM 和 Android framework。没有完成 no-VM、
Android-free 或训练准入，也未执行训练、多机放量及 10× 性能实验。
46 个排除键移除数仍为 0，可移除性仍 unknown。

起点为 `dba6d81bf8ea58413f83123d0a108955891e0f6b`，仅发布
`codex/headless-engine-plan-20261025`。阶段 SHA 以本报告所在提交及最终回执为准；
main/origin/main 保持 `0012201830cfbfcbfeb3d9dccd91a855028da869`。
此前暂停 K 的 daybreak/clapha 核验作为历史证据保留；本轮恢复 K，不把外部
bundle 未找到改写成外部引擎已运行。

## 实现

- `e2_1_pn_native_backend.js`：显式 present-ciphertext 契约，调用既有 C 派生及
  ECB/CBC codec；密钥、IV 与来源保留在私有实验目录，不进入公开报告。
- `E21PreferenceObserver.java`：受限 raw get/put 命令，只对原 store 的真实
  SharedPreferences 进行 contains/getString/edit/putString/commit；记录真实返回。
  原 App Context/Settings.Secure 身份查询仍是 Java 依赖。
- `e2_1_pn_bridge.js`：自然入口按需接原生 codec，其他实验默认路径保留。
  未覆盖的 missing、legacy-plain、empty、malformed、commit-false 和 concurrency
  显式拒收，保留部分副作用/失败现场，不伪造成功或默认值。
- `run_e2_observed.py`：冻结实际源码，等待自然写窗口最多 30 秒，拉取真实 ART
  trace；原 Java 读回前后分别保存 XML 字节。
- `e2_1k_path_trace.py`：关联原调用点、线程、阶段、返回窗口和 ART 根；具名方法、
  其他线程与 opaque method IDs 分开统计。
- `verify_e2_1k_natural_codec.py`：核对四 run、全部附件/源码身份、复编、输入、
  逐 tick 字节、控制回执、自然窗口、XML、跨进程读回与专用实例释放。

## 原生运行证据

| 独立进程 | exit | 实测秒数 | 快照 | 动作 | 终局 tick |
|---|---:|---:|---:|---:|---:|
| baseline-final | 0 | 49.2667 | 3,602 | 28 | 3,601 |
| original-final | 0 | 65.4897 | 3,602 | 28 | 3,601 |
| replacement-final-3 | 0 | 50.2871 | 3,602 | 28 | 3,601 |
| replacement-final-4 | 0 | 61.8971 | 3,602 | 28 | 3,601 |

各 run 均 9,120,440 bytes，与 baseline 直接逐字节一致，无字段归一化。
轨迹 SHA256：`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`。
17 项非 status 控制回执一致，包含动作、终局、snapshot/restore/reset。
平台 AssetManager 事件、Provider failures、live assets 各为 0；这只表示既有
资源计数覆盖的生命周期，不是 JNI 或 Android 依赖为零。
libg 九资源槽、FMOD 六绑定及 990 个 update 文件身份保持；原输入和自有实验产物
分别核验，不把新增自有产物混称为原版文件。

两次替换各 138 次桥入口：133 次非 PN 原路径 fallback 加 5 条 PN 事件。
每次均独立捕获自然 read（configuration）与 write（cold_ready_wait）；read 零 commit，
write 的两个 store 各真实 commit(true) 一次。另有 cold_ready_wait、after_reset、
final_reset_state 三次显式读回，单独标识为 probe。
自然窗口按原调用点返回指令闭合，不把未触发的 onLeave 当已观测返回。
原 Java 双 store 读回相同，读回前后 XML 原字节不变；两个独立替换进程之间的
前次写值/后次读值一致。该结果不证明掉电或崩溃持久化。

每次替换派生 1 次，安装时 8 次、最终 10 次 native transform。
ART 捕获 6 个完整同线程 nativeCommand 根；这些根内具名 Cipher.doFinal 和旧
c.a.j/n 各为 0，但全局具名 Cipher.doFinal 仍为 262。
另有 8 个 opaque method IDs，语义 unknown，exhaustive_method_coverage=false。
因此不声称“所有 Cipher 已消失”或“JNI 全覆盖”。原观察窗口包含其他键，非 PN-only。

自有观察器退休 6 个 Java 引用，JNI global 分配/删除各 1；该结论不外推整个游戏。
专用实例和 adb 已按 PID、启动时间、AVD 与端口核验后释放，5 个专用端口无 listener；
历史 adb 身份保持。

## 构建、验证与失败

三条真实复编命令 exit 0：Android ARM64 clang shared、javac --release 8、d8。
Java 编译仅出现 source/target 8 过时告警。复编及 guest 产物 SHA 一致：

- 自有桥库：`fbbebecb38bed428430f7b85df40fd8b2fca5e4dbab41834a433052ff5371c52`
- 自有 DEX：`9da012b04826378a8f2683cf20b373d727bd07ad6021913df710f3d8dce4014b`

实例释放后封存 14,707 个私有附件，限定验证 exit 0，manifest SHA256：
`ae0f4b02085ec05db0785ac1559f3b533c33caced241594a8d33bf2f997c4dff`。
公开仅保存摘要、实现和测试；原二进制、XML、身份、密钥、DEX、快照和审计留本机。
E2 全组 338 passed / 278 skipped / 16.06s / exit 0，包含本轮实际附件及损坏负测。
三个 JS 语法检查 exit 0。跳过项不算原生通过，重叠测试集合不累加。

保留两个替换失败：第一次缺 return-observer-cleanup 证据，第二次自然 PN trace
尚未结束；后者增加有界等待后最终两次运行通过。早期 trace parser 对重复 metadata
拒收，查得一个同 ID 同内容重复，新增显式允许相同重复的选项；冲突/未对齐仍拒收，
旧默认不变。另保留首次 Python 测试导入失败，修正测试导入路径后通过。
各失败未被覆盖，详见 failure index。

历史 strict gate 用其原 manifest 重验仍 exit 2/BLOCKED；它描述旧 32 键证据，
其中“PN 未实现”等旧理由保留历史含义，不能替代本轮 K 的限定结论。
K 验证器自身仍主动返回 strict_gate=BLOCKED；整体门未被下调。

## 首个未解决依赖与下一步

首个未解决依赖是非 Java 身份来源及原 store 构造，随后是 XML 持久化/backup。
missing/legacy/empty/malformed、commit-false 等价、不同值并发、崩溃持久化和
opaque 方法身份仍未闭合；46 键可移除性没有新结论。

下一阶段应在独立实例中先补 PN 失败/回退/并发语义差分，再设计显式身份和存储
契约。只替换一个依赖并维持原初始化对照；达到对应门槛后另行验证 no-VM。
本报告的运行时间是带观测器的固定轨迹耗时，不构成吞吐或 10× 提升基准。
