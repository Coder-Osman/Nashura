# E2.1-J：PN 原生派生与原 Cipher 差分

## 结论与边界

基线提交 `203357dc6eaf6416acb48ac14cefe3dbeb0e8dd4`；本阶段提交身份以本报告对应提交及最终回执为准。
任务分支为 `codex/headless-engine-plan-20261025`，main/origin/main 保持
`0012201830cfbfcbfeb3d9dccd91a855028da869`。实验仅使用本轮独占的本机 Android 实例。

**限定验证通过：从原 App 上下文取得显式身份输入后，宿主 C、Android ARM64 C 和独立
hashlib 重算派生结果一致，并与原 Java 双 store Cipher 的输出逐字节一致。**
该矩阵在完整战斗轨迹结束后执行，阶段为 `explicit_post_trajectory_derivation`。
它没有替换原自然初始化的密钥派生，也没有接管原 Cipher/XML writer。
Java 身份获取、原初始化顺序、JavaVM、配置 JNI 和 Android 框架仍保留。

46 个排除键移除数仍为 0，可移除性为 unknown。历史 targeted strict gate 重验仍为
BLOCKED/exit 2；限定证据通过与整体门分别报告。未进入 E2.2/no-VM、Android-free、
正式训练、G0/G1 放量或 10× 吞吐实验。

## 实现与输入契约

- 新增 `tools/e2_1_pn_derive.{c,h,py}`，通过显式绝对路径加载已有 crypto 库，执行原生派生。
  输入为 App 作用域身份、包名、24 项 signed int32 表和 IV 源；这些实际材料只保存在私有目录。
- 身份严格要求 16 位小写十六进制；包名为 1–255 bytes 的 ASCII 字母、数字、点或下划线；
  表恰好 24 项，IV 源为 16–4095 bytes。成功输出两份 32-byte 派生结果及 16-byte IV。
  参数、链接和 hash 失败分别返回 -1/-2/-3，输出保持原字节；拒绝输入输出及双输出重叠，
  临时变换和派生缓冲区擦除。未知身份没有合成默认值或 fallback。
- `E21PreferenceObserver.java` 增加仅 installed 后允许一次的 `derivation-source` 查询。
  来源为原 GameApp 的 App Context 和 `Settings.Secure`，不是 adb shell 的作用域标识。
  `e2_1_return_observer.js` 使用显式配置白名单，不扩大默认 RPC 权限。
- `tools/e2_1_pn_derive_probe.js` 在 guest ARM64 调用相同 C ABI，最多 16 次，输出预填
  `0xa5`，负测直接检查失败后未发布部分结果。
- `tools/e2_1_pn_derive_matrix.py` 比较原 Java Cipher、宿主 codec 与 guest 派生，保存
  双 store XML 前后原字节；恢复与 codec close 错误独立保留。
- `tools/verify_e2_1j_pn_derive.py` 独立校验附件 SHA、源码、复编产物、匹配 DEX 中的
  表/IV 来源、完整轨迹、生命周期、资源与原输入身份。拒收路径穿越、符号链接、缺失附件、
  值损坏和越界晋级声明。公开 fixture 仅验证工程行为，不代替真实附件。

## 真实完整轨迹

| 独立进程 | 原生快照 | 注入动作 | 终局 tick | 序列化 bytes | 实际平台资源事件 / Provider failures / live assets |
|---|---:|---:|---:|---:|---|
| baseline-final | 3,602 | 28 | 3,601 | 9,120,440 | 0 / 0 / 0 |
| derivation-final-1 | 3,602 | 28 | 3,601 | 9,120,440 | 0 / 0 / 0 |
| derivation-final-2 | 3,602 | 28 | 3,601 | 9,120,440 | 0 / 0 / 0 |

三个不同 PID 的全部快照直接字节比较一致，没有字段归一化。全序列化轨迹 SHA256 均为
`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`。
17 项非 status 控制回执一致，覆盖动作、终局、snapshot/restore/reset。
libg 九槽和 FMOD 六绑定验证通过；原始九项输入及 990 个 update 文件前后身份保持。
这里的零平台资源事件限定于已安装计数覆盖的完整生命周期，不表示 JNI 或 VM 调用为零。

两个派生进程各完成：双 store × 10 个文本 × key/value，共 40 项原 Cipher 对照一致；
5 项 guest 无效输入均返回 -1，64-byte keys 与 16-byte IV 全部保持 `0xa5`。
host C、guest C、hashlib 三方一致；双 store XML 前后原字节一致。
观察器 uninstall/release 成功，未据此外推游戏全部对象的生命周期或崩溃持久化。

使用已有 JDK/d8/NDK、宿主 OpenSSL 与 guest 系统 crypto 库，没有安装新的系统组件。
5 条复编命令 exit 0，4 项产物 SHA 与实用产物一致；最后一次实验后 guest 身份补验通过。
两轮早期 RPC 拒绝失败没有进入派生差分，保留原输出，不计入成功次数。

## 证据封存与实例释放

本轮独占实例已回收：cleanup exit 0，专用监听端口数 0，历史 adb 的 PID/启动时间保持。
AVD 和失败文件保留，没有清理其他任务。回收后重新封存 22,058 个静态附件，限定验证 exit 0。
最终私有 manifest SHA256 为
`9eab1cd6f1321381d7100701379e04da48e57f226a53bd28a2976b8ea079266e`。
早期运行中/回收前生成的 manifest 保留为历史，不作为最终封存清单。
公开摘要为 `e2_1j_evidence_summary.json`；原 manifest、SSAID、派生表、IV、密钥、XML、
原生快照、私有审计、DEX 和第三方二进制均留在本地。

## 实际验证

命令见 [Codex 回执](CODEX_REPLY_2026-10-27_E2_1J.md)，结果如下：

- 最终附件限定验证 exit 0；首次专项 71 passed。随后加入符号链接负测并完成 E2 全组：
  299 passed / 272 skipped / 10.90s / exit 0。跳过主要为其他阶段缺少私有附件。
- E2 首次全组 1 failed / 298 passed / 272 skipped：纯 Java 释放 fixture 缺少新增 Android
  类型声明。补齐编译声明，身份获取 fixture 显式抛出异常，未伪造默认身份；专项复跑 1 passed。
  首次失败日志保留，不把 fixture 编译失败写成原生轨迹差异。
- 公开检查 exit 0：主组 794 passed / 27 skipped；其余八组通过数依次为
  23、82、15、25、31、31、38、65，跳过数依次为 0、0、3、4、3、2、3、0。
  生成 XML 私有归档后，历史公开 XML 恢复原字节。
- 主工程 211 passed / 24.47s / exit 0；两个 JS 的 `node --check` exit 0。
- 历史 strict gate 原 manifest 重验 exit 2/BLOCKED，保留原字节和历史原因文字。
  不把旧门的原因文字冒作本轮派生差分失败。测试集合有重叠，不累加为独立原生证明。
- 源码清单、凭据扫描和提交前检查结果见本阶段最终回执。

## 已观测、未覆盖与首个依赖

**已观测并经差分验证：** 本实例非空 App 身份与匹配包名/表/IV 的显式原生派生；原双 store
Cipher 40 项输出；5 项 guest 负测；完整原生轨迹及 XML 字节保持。

**未覆盖/unknown：** 身份缺失或空值时的原 fallback，其他身份/包名的原 Java 实机对照，
自然初始化中原生派生替换，非 Java 身份来源，原生 XML writer/backup 的交接，backup
OS 交付、并发等价、崩溃持久化及 46 键可移除性。工程输入矩阵不升级为这些路径的运行证据。

首个尚未解决依赖是**原初始化集成以及脱离 Java 的身份来源契约**。下一项仍在 E2.1：
先以显式、已核验来源接合原生派生与 codec，独立核验初始化顺序与原输出，再逐项处理
XML 持久化、backup、并发和失败契约；未知请求显式失败。不以本阶段结果启动 E2.2/no-VM。
