# E2.1-E：PN_POPUP 独立原生持久化候选

## 结论与范围

本阶段完成窄 C ABI 原生存储候选，以及保留 JavaVM、原配置 JNI 和初始化顺序的
独占实例差分。限定附件验证 exit 0；G-E2.1 严格门重验仍为 BLOCKED / exit 2。
PN_POPUP **尚未接入 native replacement**，46 个排除键全部保留，可移除性 unknown。

起点提交 `8c000dd73b3d342199d765db33534dbca2766f37`；阶段 SHA 以对应 Git 提交为准。
仅更新 `codex/headless-engine-plan-20261025`，main/origin/main 保持
`0012201830cfbfcbfeb3d9dccd91a855028da869`。

候选在原版完整轨迹、reset 及原桥语义 probe **之后**加载和独立重放。
完整轨迹对照证明原版基线仍有效，不能解释为候选接管战斗后的语义一致性。

## 实现

- `tools/e2_1_pn_store.h/.c`：open/read/write/close/free，错误与提交 receipt 分离。
- `tools/e2_1_pn_candidate.js`：仅在独占 app 目录加载自有库，保留原 JNI 桥。
- `tools/run_e2_observed.py`：可选 post-trajectory 重放，默认路径保持原样。
- `tools/verify_e2_1e_candidate.py`：绑定 14,604 个本地附件及最终源码，校验原生轨迹、
  输入身份、构建/安装身份、原桥实际返回字节与候选 receipt；输出白名单摘要。
- 两个独立测试文件：宿主存储测试与真实附件/负向突变验证。

候选使用自有双文件格式，带 magic、版本和长度，最大 4095 个非 NUL 字节。
它**不兼容原 Cipher/XML**，没有读取或覆盖原 SharedPreferences 文件。
UTF-8 BMP 输入已对照；补充平面、嵌入 NUL、非法编码及旧格式迁移不在真实差分覆盖内。

打开时检查 regular file、O_NOFOLLOW 和独占 lease。生命周期读写锁配合各 store 的锁，
并未建立跨两个 store 的整体原子事务。close 等待当前操作，后续读返回 CLOSED；
free 的调用前提是不存在后续或并发使用。游戏 reset 与该对象 close/free 是不同概念。

候选按已观测状态机：p 优先、p 空时读 o；非空 o 回填 p；比较后仅变化才提交。
先更新内存，再写临时文件、fsync、rename 和目录 fsync。提交失败保留内存新值，
receipt 显式报告失败及 errno；write ABI 返回成功表示调用完成，不表示磁盘提交成功。
同值写不重试此前失败。backup_requested 只记录意图，真实 backup 交付仍 unknown。
这是待验证候选设计，不能用合成测试证明原桥在全部场景下等价。

## 原生结果与身份

baseline 与 candidate-3 使用两个独立原版进程，各自产生：

| 项目 | 实际结果 |
|---|---|
| 原生快照 | 3,602 份，9,120,440 bytes，逐字节一致 |
| 动作/终局 | 28 动作，tick 3601 原生终局 |
| 生命周期回执 | snapshot / restore / reset 及控制响应一致 |
| 轨迹 SHA256 | `0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5` |
| 资源路径 | libg 九导入槽、FMOD 六绑定通过已有资源校验 |
| 平台 AssetManager 事件 | 0；Provider failures 0；live assets 0 |
| 原始输入 | 原 APK、libg、libfmod、probe、Provider 及 990 个 update 文件身份保持 |

自有 ARM64 库使用已有 NDK API30 clang 实际构建并复编，两个产物字节一致：
`d7000052d6b965364cbaced93b4aa59c916ceb29a011d21f9c1f10202264d6b5`。
安装后 guest SHA 核验一致。二进制、私有附件和配置值仅本地留存。

原桥 17 行证据含保存/恢复，候选重放其中 14 行、7 次实际读取：
删除、普通值、同值、不透明字符串、BMP Unicode、4095 字节、空值均与原桥返回字节对应。
原桥 probe 前后 pending JNI exception 均为 false，保存值恢复通过。
实际 app 权限故障使两个候选提交 errno=EACCES；内存读到新值，同值再写零提交；
恢复目录权限并 close/open 后读到故障前磁盘值。普通 close/open 读值也一致；
三次 close 后实际读取均返回 CLOSED。

真实原桥 p/o 冲突、仅 o 有值的回填、null 输入的跨桥差分仍未覆盖。
宿主 fixture 的 p 优先与回填测试仅为候选工程检查。

## 失败与证据边界

首次宿主编译缺平台宏，保留日志后补齐宏并通过严格编译。
candidate-1 的完整轨迹之后，自有库在临时目录加载遭 SELinux execute 拒绝，exit 1；
没有关闭安全保护。改用该独占实例已有 app native library 目录新增自有库、恢复正确标签，
原文件保持原样；candidate-2 和 candidate-3 exit 0。第二次成功运行的重开证据较弱，
最终限定验证采用第三次实际 before/after 读值，不用自报 equal 代替证据。
失败记录、命令、退出码、stdout/stderr 和早期成功均保留。

独占实例已回收，专用端口 listener 为 0；历史 5037 adb 身份保持。
未触碰冻结任务、训练后端、G0/G1 目录；未安装新游戏版本，未修改原始引擎和资源。

## 验证与后续

44 项本阶段专项通过；E2 回归 175 passed / 74 skipped；主工程 211 passed。
跳过项包括缺历史外部附件的测试，不把 skip 作为原生通过。公开主组
794 passed / 27 skipped，另八组公开专项 exit 0；命令与各组结果见本阶段 Codex 回执。
原始附件未公开，公开摘要不能单独重验原生结果；验证器缺附件时显式失败，相关测试显式跳过。

首个未解决依赖是 PN 原桥集成及原 Cipher/XML 存储迁移契约，而非候选函数可否编译。
后续先在保留 VM 的独占实例补齐原桥冲突/回填与失败差分，再明确存储交接及 backup
契约，之后才考虑接入并重跑完整轨迹。并发等价、崩溃持久化及 46 键移除性继续 unknown。
本轮未进入 E2.2、no-VM、Android-free、正式训练或 10× 实验。
