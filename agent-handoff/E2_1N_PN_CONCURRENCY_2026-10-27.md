# E2.1-N：PN 并发归因与有界原生串行门

## 结论与范围

限定诊断验证通过，**E2.1 strict 仍为 BLOCKED**。本阶段没有移除 Java VM、
Android、Java 身份获取、原 store/XML 或 backup；46 个排除键移除数仍为 0。
验证的是已有固定完整轨迹和两线程有界 PN 实验，不是通用并发等价、no-VM、
Android-free、训练准入或吞吐提升。

基线提交 `256eaa6db54bd02539188fd4e6d7d7c42aaca72d`。只在既有任务分支和
本轮独占实例中工作，main、冻结部署及训练目录保持原样。

## 真实观测

1. 原版 PN 的具名 Java 方法未设置 `synchronized` 修饰符。观测 DEX 自身的
   command/record/nextEditor 有该修饰符；这只是反射元数据，内部同步块、锁获取
   顺序和 Cipher 的通用线程安全性仍 unknown。
2. 只读原生入口/实际 caller return instruction 观测关闭了 18 次调用，记录
   8 次跨线程入口重叠。两个显式 worker 各完成四轮 write/read，未知读回为 0，
   detach 成功。另三个外部 probe 返回点超出有界可执行覆盖，保留 partial/unknown，
   没有扩大函数边界或推断返回。该实验不证明游戏自然 Mainloop 本身一定并发读写 PN。
3. 无串行门的替换尝试在 `jni:native-get` 内出现另一线程 read 重入，后端显式拒绝，
   上层记录一次 bridge contract violation。实验在 600.015 秒外部超时退出 124。
   **拒绝已观测；超时原因和进程 abort 未确认。** 不能将此归类为纯超时，也不能
   沿用 M 阶段另一尝试的 abort 结论。首局完成；race 最终结果、恢复存储、第二局、
   最终身份均未覆盖。
4. 新增显式可选 `native-serial-v1` 诊断模式：pthread errorcheck mutex 在进入
   JavaScript callback 前串行化配置桥 dispatch。非 PN fallback 在释放该门之后
   调用原桥；不是整个配置/JNI/XML 路径的锁。递归进入返回 -11，其他门错误返回 -12，
   不伪造原操作成功。
5. 两个独立替换进程在此模式下完成冷启动、首局、双 worker 竞争、存储语义恢复、
   第二完整生命周期。每进程 entered/completed=154，active/waiting/lock_errors=0，
   peak_waiting=1；PN 后端调用分别 44/48，拒绝与桥违约均 0。peak_waiting 是进入
   mutex 前计数器峰值，不等于已证明的阻塞线程数。每个 worker 四次 write/read，
   各进程合计 own_reads=4、other_reads=4、unknown_reads=0。

这是显式实验开关，不是自动部署的通用修复。全局串行会改变调度，是否符合原版可观察
并发语义尚未证明；不把两次成功写成并发问题完全解决。

## 完整状态与资源对照

封存清单仅接收五个指定尝试：原版 control、原版 timeline、拒绝后超时替换、
串行替换与独立串行复验。共八条完整轨迹各 3,602 快照、28 动作、9,120,440 bytes，
tick 3,601 终局，全字节一致：

`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`

首生命周期的 17 项非 status 控制回执跨进程比较；第二生命周期在原版和两次替换间
单独比较，保留真实递增 generation/configRevision/stateEpoch/handle，不抹除字段。
snapshot/restore/reset 均在成功完整生命周期中核验。首局与第二局不能直接要求
运行代次标识相等。

四个成功尝试的已有资源门核验通过：九个 libg 资源槽、六个 FMOD 绑定保持，
实际平台 AssetManager 事件 0、Provider failures 0、live_assets 0。
这些零值限定于既有资源计数覆盖，不外推所有 JNI/Android 调用。
每个串行替换仍有 133 次非 PN 原桥 fallback 和 21 次 PN 桥事件。

APK、libg、libfmod、原 probe、Provider 及 990 个 update 文件在可完成的前后身份
核验中保持。跨尝试仅允许已绑定的新增自有 `libe21dispatchgate.so`，其他输入和
observer 身份必须相同；超时尝试没有最终身份，明确保持未覆盖。

## 源码与证据

- `e2_1_pn_timeline.js`：只读入口/实际返回点及线程时间线；未覆盖显式记录。
- `e2_1_pn_dispatch_gate.c`：进入 JS 前的可选原生串行门与独立计数。
- PN bridge/backend：有界 dispatch/JNI/codec 分阶段事件，流与最终快照逐项核对。
- return observer：在 module observer 注册前预加载自有库，避免 loader 回调内嵌套加载。
- runner：显式竞争、存储保存/恢复、第二生命周期与独占进程清理证据。
- `verify_e2_1n_concurrency.py`：绑定 29,164 份附件和五份验证源码闭包；保留
  rejection、timeout、partial coverage 以及各独立 gate 的边界。

既有 NDK/JDK/d8 独立复编三条命令均 exit 0，未重新部署或更改 guest，产物与
先前部署字节相同：

| 自有产物 | SHA256 |
|---|---|
| 串行门 SO | 755929d0ec0681cf28a5610f20f0cd098f52f507df3f5cc442d1c0d54199b7d6 |
| 锁元数据观测 DEX | 70b37ac6c4d63400a70a61bd4f540f018f194c3a5e4cf7a35d69b448877853e7 |

私有清单 SHA256：`69be0614f786ffacdbd166cf7b06dc409cf0265c9bc212609ffdce0656037d2c`。
只公开脱敏摘要、源码及测试；配置、原始调用日志、XML、身份输入、二进制和快照留本机。
成功尝试所属进程已按 PID/starttime 清理，独占 AVD/adb 保留供续验。

## 失败记录与验证边界

三次失败尝试分别保留：重入拒绝后外部 timeout/124；初始化最终证据缺失/1；
module loader 回调内加载实验被外部终止/-9。后者的线程快照有 linker 锁等待，
精确死锁因果仍 unknown。修改为 configure 阶段预加载后，两次独立实验成功，
不覆盖先前记录。

统一验证器的早期失败也保留：receipt 字面转义、错误跨生命周期比较、误选初始化
失败尝试、忽略已观测拒绝、以及把新增自有门误判为保护输入变化。最终逐项修正证据
引用和校验，未修改原附件或放松原输入身份门。详见失败索引。

实际命令与工程/公开检查见同阶段 Codex 回执。工程 fixture 不证明原生并发等价；
缺少其他阶段私有 manifest 的 skip 不计作原生覆盖。历史整体 strict exit 2 保留，
本阶段限定验证 exit 0 不替代整体 strict。

## 首个未解决依赖与下一步

下一项先验证原版和替换在真实 commit(false) 及其副作用上的差异，区分内存可见、
磁盘持久化、第二 store、backup 调用和返回值，保留错误状态，不统一制造成功。
对原版自然并发/内部锁、非 Java 身份、store/XML/backup、崩溃恢复仍分别建立证据。
串行模式只能用于明确标识的后续诊断，不视为已获通用等价或训练准入。
