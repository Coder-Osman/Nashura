# E2.1-I：自然冷启动 PN 原生控制桥

## 结论与边界

基线提交 `e61fe488cccc9f5197acd0aecbd2b0321dc486a6`；本阶段 SHA 以本报告对应提交和最终回执为准。
工作分支仍为 `codex/headless-engine-plan-20261025`，main/origin/main 保持
`0012201830cfbfcbfeb3d9dccd91a855028da869`。仅对本轮独占本机 Android 实例实验。

**已验证：原版自然冷启动的 PN_POPUP 读写由原生双 store 控制逻辑接管，保留原 Java
逐 store Cipher/XML/backup 后端。** 两次独立替换进程和两个对照进程完成同一原生完整轨迹，
全部快照字节一致；正常独立进程重启后的 PN 读回一致。

这是限定控制逻辑替换，未完成原生密钥派生或原 XML writer 交接。JavaVM、配置 JNI、
Android 框架和原初始化顺序保留；本轮未同时启用已有 32 键替换。46 键可移除性仍为
unknown，移除数 0。strict gate 仍 BLOCKED/exit 2；没有 E2.2/no-VM、Android-free、
训练准入、G0/G1 放量或 10× 完整对局加速通过声明。

## 实现

- `tools/e2_1_pn_bridge.js` 增加显式 `first-native-bridge` 安装契约和生命周期回调，
  记录本地私有事件的来源、输入、结果及原调用点；保留 H 阶段默认 post-cold-ready 行为。
- `tools/e2_1_return_observer.js` 在注册 libg module observer 之前预加载自有桥库，
  首个真实配置入口安装 Java 观察后端；重入显式失败。自然返回在原调用点返回指令记录，
  不在同一函数入口叠加 attach 与 replaceFast。外部显式 probe 独立计数。
- `tools/run_e2_observed.py` 校验桥与观察器阶段一致，拒绝同时启用 32 键替换；
  冷安装状态由早装观察器回收，失败路径也尝试保存和退休。一般 detach 停滞尚未修复。
- 新增 `tools/verify_e2_1i_cold_pn_bridge.py`、独立 Python 负测及 JS 工程 harness。
  验证器校验真实附件及源码身份、自然入口/返回关联、提交顺序、资源、生命周期和跨进程读回，
  对缺失证据或不支持的晋级声明显式拒收。fixture 只验证工程行为，不充当真实游戏证据。

## 完整轨迹与身份

| 独立进程 | 原生快照 | 动作 | 终局 tick | 序列化 bytes | 实际平台资源事件/Provider failures/live assets |
|---|---:|---:|---:|---:|---|
| baseline-final | 3,602 | 28 | 3,601 | 9,120,440 | 0 / 0 / 0 |
| original-final | 3,602 | 28 | 3,601 | 9,120,440 | 0 / 0 / 0 |
| replacement-final-3 | 3,602 | 28 | 3,601 | 9,120,440 | 0 / 0 / 0 |
| replacement-final-4 | 3,602 | 28 | 3,601 | 9,120,440 | 0 / 0 / 0 |

四个进程全序列化字节 SHA256 均为
`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`，
无字段归一化；action、snapshot/restore、reset 和 terminal 控制回执一致。
libg 九槽、FMOD 六槽验证通过。原始九项输入和 990 个 update 文件每轮前后身份保持。

自有桥库和 dex 使用现有 NDK/JDK/d8 构建，11 条命令 exit 0；复编产物与 guest 身份一致，
补验最后一次 guest 身份通过。桥库 SHA256 为
`916bf406ea4e265201894dfc69f69021afc6a9926acad4f87da3e4aea6311523`，
dex 为 `ffe97b238e6ac11eb1fdce09b13e5c5a1d100360dd6e686dc57c9ce80a36bfb7`。
构建命令、输入和原始结果只保存在私有实验目录；公开仓库仅含源码与脱敏摘要。

## 自然读写与阶段归因

每个替换进程均有 138 次冷配置桥入口：133 次非 PN 原桥 fallback，加上 5 条 PN 事件。
其中 2 条为自然冷启动，3 条为显式读回。fallback 的 133 次不表示 133 个不同键，也不表示
这些键均为战斗必需；本轮未移除这些路径。

| 自然事件 | 独立观察的内部阶段 | 原调用点返回位置 | 上层独立 caller | 双 store 提交 |
|---|---|---|---|---|
| PN read | configuration | libg.so+0x1394198 | libg.so+0x76cae0 | 0 |
| PN write | cold_ready_wait | libg.so+0x13942f0 | libg.so+0x76cfcc | o/p 各 commit(true) 1 次 |

两个进程自然 read/write 分别位于观测入口序号 34/102；这些是本次观测值，不是通用 ABI。
原调用点、线程、基址和上层 caller 相互匹配，转发后端的真实操作顺序与原桥一致。
自然值为十进制文本，write=read+1；不发布其实际值。

自然 read 的原生 receipt 为 reads=1、writes=0、changed=0、backup_method_returns=0、failure=0。
自然 write 为 reads=2、writes=2、changed=3、backup_method_returns=1、failure=0。
backup 方法正常返回不等同于 OS 完成交付。

显式三次 read 分别归于 cold_ready_wait、after_reset 和 final_reset_state，均等于自然 write。
replacement-final-3 的终局读回与 replacement-final-4 的自然 read 相等；读回同时检查
字符串字节和显式 JNIEnv.ExceptionCheck 的 before/after=false 及其来源字段。

桥入口 onLeave 计数仍为 0；自然返回由原调用点返回指令的 after/completion 证明，
不把 0 解释为没有返回。宿主控制器的 cold-ready 等待区间也不等同于内部 configuration 阶段。
本轮重入计数为 0，尚未证明并发等价。

## 引用与实例生命周期

每次替换保留 10 个桥强引用至退休；恢复原后端并释放自有 6 个 Java 引用，
JNI global reference 分配/删除各 1。桥退休无错误；不据此外推原游戏全部对象的生命周期。

本轮独占实例已回收：cleanup exit 0，专用监听端口为 0，历史 adb 原身份保持。
独立 AVD 目录及历史失败记录保留；未接管或清理其他任务。

## 实际验证

命令和退出码见 [Codex 回执](CODEX_REPLY_2026-10-27_E2_1I.md)。

- 14,651 附件的限定验证 exit 0，脱敏摘要为 `e2_1i_evidence_summary.json`。
- E2 全组 280 passed / 218 skipped / 29.21s / exit 0。跳过主要是其他阶段缺少私有附件，
  不计作原生通过；各测试集合重叠，不累加为独立用例数。
- 公开检查主组 794 passed / 27 skipped；其他八组均 exit 0。主工程 211 passed / exit 0。
- 历史严格门 manifest 原字节重验仍 exit 2/BLOCKED，未改写旧 gate 或失败报告。
  旧门的历史原因文字不代表本轮 PN 尚未接管，当前限定证据独立发布。
- gitleaks exit 0；公开检查生成 XML 私有归档后，历史公开 XML 原字节恢复。

私有 manifest SHA256：`91d9bf85088ae0693539f186ac8b8dedf963343ebb4bafcb2f4587c5fcdbddb1`。
私有 manifest、配置值、密钥/IV、SSAID、XML、原始快照、汇编和第三方二进制均不发布。

## 失败与未知

`e2_1i_failure_index.json` 保留失败分类。前两次替换在 cold ready 前停滞，桥已 armed，
尚未进入首次回调，也没有快照。预加载自有桥库后成功；怀疑 moduleAdded 内首次嵌套
Module.load 阻塞，但确切锁关系 unknown。一般 detach 停滞也保持未解决；保存证据后
核验所属 PID/启动时间释放本轮实例解除，未影响历史进程。

首次限定验证失败来自验证器遗漏 ExceptionCheck 的 source 字段，不是自然读回值差异。
修正为验证完整异常状态并补充来源/异常/读回值损坏负测，保留原失败和首次专项失败日志。
另保留错误 cwd 和 zsh 只读变量引起的辅助命令失败，修正后重新执行通过。

## 首个未解决依赖与下一步

首个尚未剥离的 PN 依赖是 **Java 侧密钥派生及逐 store Cipher/XML 持久化**。
下一项仍限定 E2.1：明确密钥来源与失败契约，接合已经有差分证据的原生 codec 和存储候选，
先比较原后端的冲突/回填、真实 commit 失败、关闭/重开，再验证自然冷启动完整轨迹。
未知密钥、资源和状态显式失败，保留原初始化顺序，不伪造默认值。

backup 交付、并发与崩溃持久化、46 键可移除性及初始化对象 ABI 仍需独立证据。
限定门、整体 strict gate、no-VM、Android-free 和训练准入分别验收；本轮不进入后续阶段。
