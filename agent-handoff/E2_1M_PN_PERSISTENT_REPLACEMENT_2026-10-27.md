# E2.1-M：自然 PN 持久解密替换与异常后完整轨迹

## 结论与验收边界

基线为 `abe4fc1451e10682654beeaf1ba67b865131830b`，工作分支为
`codex/headless-engine-plan-20261025`。本轮将 L 阶段观测到的持久解密、异常清理和
回退行为接入自然 PN 替换入口，限定附件验证结果为 `bounded_replacement_verified`。
这推进了真实替换；E2.1 整体 strict 仍为 BLOCKED，不是 no-VM 或 Android-free 验收。

五组原版／替换对照共十个独立游戏进程，每个进程依次执行：

1. 固定 28 动作完整对局及 snapshot/restore/reset。
2. 有界 PN 异常矩阵，记录原生返回、真实 raw 操作、commit 和 XML 条目。
3. 恢复矩阵前的 PN 存储条目语义，保留该进程实际解密上下文状态。
4. 再次执行完整对局及 snapshot/restore/reset。

共二十条轨迹各 3,602 快照、9,120,440 bytes，28 动作和 tick 3,601 终局全字节一致。
**这是恢复存储后、保留异常解密状态的完整局证据；未证明损坏存储不恢复的完整局。**
这些都是相同固定轨迹的对照，不是二十个新种子自博弈，也不是吞吐基准。

Java 身份获取、原 store 构造、SharedPreferences/XML、backup、Java VM 和 Android
仍保留。每个替换 run 仍有 133 次非 PN 原桥 fallback。46 个排除键移除数为 0，
可移除性 unknown。未执行 E2.2、正式训练、多机放量或 10× 性能实验。

## 实现

- `e2_1_pn_native_backend.js`：新增显式 `persistent-pn-edges-v1`，保持旧 K 模式契约。
  两 store 各持久 EVP 解密上下文；失败保留状态，成功 final 后按原 key/IV 初始化。
  缺失返回空；plain 旧键忽略并保留；解密失败或有效空输出走真实 remove/commit。
  空写入真实删除，非空写入沿用 stateless CBC；commit(false) 显式失败，保留已发生副作用。
- `E21PreferenceObserver.java`：新增具名 `native-remove:<store>`，直接调用原
  SharedPreferences 的 remove 和同步 commit，返回真实布尔值，不制造成功回执。
- `e2_1_pn_bridge.js`：新模式独立版本身份，保留 natural caller、线程、phase、
  原生操作 receipt、strict BLOCKED 和未覆盖项。
- `run_e2_observed.py`：新增异常后第二生命周期，17 项非 status 控制回执、快照
  分支重放和完整轨迹分别保存；必须显式启用异常矩阵及第二生命周期。
- `verify_e2_1m_persistent_bridge.py`：绑定 73,183 份私有附件，核验成功 attempt、
  运行与构建源码、五组矩阵、自然调用归因、二十轨迹、资源、输入身份和进程清理。
  对 bridge/backend 前缀连续性、单次事件、原生 receipt、真实 commit、XML 和来源逐项校验。

实现仅接受已观测的 `%%%` 非法 Base64 行为及 canonical Base64；其他编码、二进制或
非法 UTF-8、commit-false 等价、并发等价继续显式标为未覆盖。`%%%` 的独立原版证据
不外推所有非法 Base64。JS 工程 fixture 的 stream 为 stateless 路由桩，未作为持久
Cipher 语义证据；真实语义来自下列原版／替换差分及 L 阶段原 Cipher 差分。

## 真实差分与完整生命周期

| 序列 | 每方矩阵行数 | 原版成功 run | 替换成功 run |
|---|---:|---|---|
| edges | 19 | original-edges-retry1 | replacement-edges |
| malformed-first | 2 | original-malformed-first-retry1 | replacement-malformed-first |
| padding-first | 3 | original-padding-first-retry1 | replacement-padding-first-retry1 |
| short-state | 3 | original-short-state-retry1 | replacement-short-state |
| padding-state | 4 | original-padding-state-retry1 | replacement-padding-state |

各组原生 read/write 返回、raw 变化、commit 次数及 XML 条目语义一致；非 PN 条目保持。
矩阵前后存储按逻辑条目恢复，不声称 XML 序列化字节完全恢复。自然冷启动 read/write
分别关联 caller、return address、线程和 phase，原始上层 observer 独立核对。

每条完整轨迹 SHA256：
`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`。
前后两生命周期各 17 项非 status 控制回执一致；snapshot/restore/reset 与终局均通过。
九个 libg 资源槽、六个 FMOD 绑定保持；实际平台 AssetManager 事件 0，Provider
failures 0，live_assets 0。该零值仅在已有资源计数覆盖内，不外推所有 Android/JNI。

原冻结七项输入加本轮自有 SO/DEX 共九项身份、990 个 update 文件前后均保持。
原 APK、libg、libfmod、probe 和 Provider 未改；只部署本轮自有桥库和观测 DEX。
NDK ARM64 clang、javac --release 8、d8 三条独立复编命令均 exit 0，SO/DEX 与部署字节一致。

| 自有产物 | SHA256 |
|---|---|
| 桥 SO | af569a0ae91be4b42d82084f6a36306dbeabc6f24631f4b4a2989318d4b5afe6 |
| 观测 DEX | c1fdbaef8c5029433dfff9dd39770a61c598a11524843f401745f1a975c04dc4 |

自有二进制、原始 XML、身份输入、密钥、逐 tick 快照与私有审计均留本机。
私有 manifest SHA256 为
`b550a110702b9afe5e41e328e4b501844c901174ddb46a6298982e3316fa24ea`。
验证器及依赖源码已按 manifest 独立冻结，后续测试日志另存，不覆盖此封存身份。

## 失败与覆盖缺口

五个首轮原版尝试因旧审计输出路径冲突 exit 1；修正为独立输出，五个 retry 各 exit 0。
首次 replacement-padding-first 因外部 semantics-cold probe 与 Mainloop 后端调用重叠，
触发 `bounded serial PN backend required`，桥返回 -10，进程 abort，attempt exit 1。
该失败在异常矩阵之前，不是 bad-padding 差分失败。全部原始日志和失败输出保留。
后来独立 retry 成功不消除这个缺口，`concurrency_equivalence=unknown`。

当前后端是有界串行实验实现。未证明不同线程同时进入、重入、commit(false)、
进程崩溃恢复和 backup 最终交付等价；不以重试成功替代上述证明。
成功 run 的所属游戏进程均按 PID/starttime 清理；本轮独占 AVD/adb 保留供续验，
历史任务不变，不声称整个实例已回收。

## 实际验证

命令与退出码详见同阶段 Codex 回执。M 私有附件验证 exit 0；专项 48 passed / 11.54s。
E2 回归 356 passed / 350 skipped / 149 deselected / 15.66s；另用现有 JDK 显式运行
Java 释放工程测试，1 passed / 0.61s。集合重叠，不累加；缺其他阶段私有 manifest 的
skip 不作为原生覆盖。发布检查结果单独记载在回执。

## 首个未解决依赖与下一步

本轮闭合了已观测异常序列的自然 PN 持久替换及恢复存储后的完整生命周期。
首个已复现缺口是外部 probe 与 Mainloop 并发重入。下一项先独立观察原实现的
线程与锁边界，区分工具诱发重入和游戏实际并发，再据证据设计等价实现；不静默
加全局锁、不跳过初始化、不关闭 Mainloop 来获得通过。

随后分别验证 commit(false)、非 Java 身份、store/XML/backup 和崩溃持久化契约。
E2.1 strict、fresh-process no-VM、Android-free 完整生命周期保持独立门槛；本轮
未重跑整体 strict CLI，也未将限定验证器 exit 0 替代历史 strict exit 2。
