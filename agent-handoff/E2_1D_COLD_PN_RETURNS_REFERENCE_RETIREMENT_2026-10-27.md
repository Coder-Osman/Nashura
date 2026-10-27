# E2.1-D：自然冷启动PN返回与观测器引用退休

日期：2026-10-27。工作分支 `codex/headless-engine-plan-20261025`，起点
`e58888b4da023f1d26f29935083ae7cbcc653db6`。本阶段属于E2.1限定补验，严格G-E2.1仍BLOCKED。

## 本轮原生结果

使用本机已有ARM64 Android环境的独占克隆，保留JavaVM、原配置JNI和初始化顺序。
本轮配置为original；未将PN_POPUP接入native replacement，也未移除46个排除键。
原始APK、libg、libfmod、probe、Provider、资源及990个update文件身份前后保持一致；
冷启动观测仅额外部署本项目自有DEX，单独记录其身份，不冒充原版文件。

| 运行 | 原生结果 | 返回观测 | 命令退出码 |
|---|---|---|---|
| baseline | 完整轨迹 | 无临时返回观测器 | 0 |
| cold-1 | 完整轨迹一致 | 缺after，不采纳返回证据 | 0，旧runner未拒收 |
| cold-2 | 完整轨迹一致 | 缺after，明确拒收 | 1 |
| cold-3 | 完整轨迹一致 | 自然读/写两个窗口完成 | 0 |
| cold-4 | 完整轨迹一致 | 第二独立进程复现 | 0 |

每个完整轨迹均为3602个原生快照、9120440 bytes、28个动作、tick3601终局；
snapshot/restore/reset及17项控制回执一致。比较完整序列化字节，无字段归一化。
轨迹SHA256：`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`。
独立D验证器接收baseline/cold-3/cold-4，不把前两次观测失败算作返回验收成功。
两次失败的轨迹比较另有私有完整记录；公开摘要只导出白名单计数。

两次成功冷启动均在第一个真实配置桥入口安装临时转发观测器。
自然PN读取来自原libg调用点 `0x1394198`、configuration阶段，零commit；
自然PN写入来自 `0x13942f0`、cold_ready_wait阶段，两个原store各一次commit(true)。
原store、Editor及commit均真实委托执行，保持原返回值和异常，不记录配置明文。
独立配置观测器对调用来源、native线程及阶段逐项关联；窗口内Java线程单一，
Java线程ID与Linux TID分别处理，不推定两者数值相等。

cold-3终局reset后的显式读取，与停止该所属进程后cold-4的自然configuration读取一致。
这是正常进程重新启动的读回证据；不证明断电、崩溃一致性或双store原子性。

## 观测边界与代码

早安装时原桥Interceptor.onLeave计数为0，晚安装独立诊断可观测到返回，原因仍unknown。
没有扩大函数边界或伪造返回：早装方案在原桥returnAddress对应的原引擎调用点挂只读钩子，
实际执行返回点时读取转发事件快照。外部直接probe的三个窗口独立计数，不混入自然调用。

两个自然返回点相对caller及推导出的页对齐模块基址一致；另有独立caller/线程/阶段证据。
当前未归档独立的绝对模块基址回执，故不将该检查扩大为任意调用地址的完整证明。
早于首个native配置桥的Java访问、其他键及未配置间接调用均未由此观测器穷尽覆盖。

结束时先恢复两个原store引用，再清除观测器自己的6个显式Java引用及反射Field，
删除1个JNI global reference，后续helper命令明确拒绝。该结果只覆盖观测器自有引用，
不证明GC、类卸载、游戏配置对象销毁或其他线程持有的旧proxy全部消失。

新增独立 `verify_e2_1d_evidence.py`，绑定18307个私有附件及源码身份，检查完整轨迹、
输入、资源、两个自然窗口、真实commit结果、引用退休和跨进程读回；空窗口明确拒收。
runner的completed只是运行状态，单独使用它不构成自然窗口验收；必须运行该验证器。
旧C验证器和严格G-E2.1保持原定义，不用新源码覆盖旧归档身份。

`e2_1d_public_summary.py`仅导出固定字段和计数；私有值、路径、原始trace、DEX及第三方
二进制均不发布。私有manifest v3摘要：
`6dcb81a9f2e090a105c71557177d50b793019b532a4655a3980813e2c01f1899`。

## 实际验证与剩余依赖

- D限定验证：exit0；新增专项25 passed/3.58s，含实际附件的负向突变。
- E2专项：146 passed、59 skipped/6.27s；跳过的是未提供对应外部附件等测试，不计为通过。
- 主工程：211 passed/38.16s。公开回归及发布检查见同阶段Codex回执。
- 原严格门复验：exit2/BLOCKED；不晋级no-VM、Android-free、训练或10×。
- 独占engine、Frida、AVD和专用adb已回收，五个专用端口无监听；原5037 adb身份保持一致。
- 所有失败和未知状态保留，见 `e2_1d_failure_index.json`。

首个剩余依赖仍为PN的native持久化与失败等价契约：原JNI在本轮继续提供实际存储。
下一步应从已观测的正常/同值/空值/commit(false)/内存可见性/跨进程读回出发，
先定义独立的持久化状态机及失败传播，再在保留JavaVM的隔离实例做原桥对照。
46键消费者与移除反事实、游戏Java对象生命周期仍unknown；不得用统一默认值绕过。
本轮未进入E2.2、无VM冷启动、正式训练或吞吐放量。
