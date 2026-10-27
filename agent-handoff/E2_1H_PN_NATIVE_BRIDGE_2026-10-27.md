# E2.1-H：PN控制逻辑接入原生桥，保留Java逐store后端

日期：2026-10-27。实现基线：`65930c7337901611c48ebe9403e6f76ef717f74c`。
本阶段提交SHA以本文件对应提交和Codex最终回执为准，仅发布任务分支
`codex/headless-engine-plan-20261025`；main/origin/main保持
`0012201830cfbfcbfeb3d9dccd91a855028da869`。

## 结论与边界

限定证据验证通过，整体E2.1严格门仍BLOCKED/exit 2。本轮把PN_POPUP的双store
优先级、回填、变化判断和backup调用调度迁至C，在post-cold-ready、state_create前
接入原生读写入口。每个store仍调用原Java的读取/写入方法，保留Cipher、
SharedPreferences缓存、XML writer、GameApp.requestBackup、JavaVM和原初始化顺序。
本轮与32键替换实验隔离，未同时启用后者，也未移除46个排除键。

这不是完整native持久化或无VM实现。13条替换桥PN事件均来自显式probe：
2次生命周期读取和11项顺序矩阵；本次固定战斗轨迹未观察到自然PN调用。
因此只证明所覆盖的显式调用与固定轨迹对照，未证明自然冷启动替换、并发等价或
任意输入轨迹等价。实验Java命令adapter有额外同步，不能作为原后端并发语义证据。

## 实现

- `e2_1_pn_bridge.c/.h`：无缓存、无持久化的控制逻辑；标准UTF-8、最多4095bytes，
  空串可用；拒绝非法编码、超长、无终止输入。后端错误显式传播，不输出部分读结果。
- 读先p，p非空直接返回；否则读o，o非空才回填p并调用backup。
- 写先读o、必要时写o，之后才读p、必要时写p；任一变化仅调用一次backup。
  保留原顺序，不预读双store或新增全局锁。
- receipt区分读取数、writer方法返回、变化mask、backup方法返回与错误。
  writer原方法返回void，真实commit另从SharedPreferences观测，二者不混用。
- `e2_1_pn_bridge.js`：仅匹配PN_POPUP，其他键走原trampoline；失败显式终止实验，
  不伪造默认值。强引用保留回调、代码和backend；退休恢复两个原入口。
- `e2_1_backup_trace.py`：解析真实ART SLOW v3/dual trace，只解释两个具名backup
  方法；未知方法、异常退出和截断保持独立状态，不声称覆盖全部Java行为。
- `verify_e2_1h_pn_bridge.py`：校验14744附件、四个独立进程、归档源码、实际构建、
  原始输入、轨迹、控制回执、XML、matrix及ART；输出限定白名单摘要。

## 原生对照与身份

| 独立进程 | 原生快照 | 动作 | 终局tick | 结果 |
|---|---:|---:|---:|---|
| baseline-final | 3602 | 28 | 3601 | exit 0 |
| original-final | 3602 | 28 | 3601 | exit 0 |
| replacement-retained-1 | 3602 | 28 | 3601 | exit 0 |
| replacement-final-2 | 3602 | 28 | 3601 | exit 0 |

每份完整轨迹9120440bytes，逐字节一致，SHA256：
`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`。
snapshot/restore/reset、动作与原生终局控制回执一致。每轮实际平台AssetManager
事件0，Provider failures=0、live_assets=0；9个原输入及990个update文件前后身份保持。

新增桥库与observer dex用现有NDK、JDK/d8复编，SHA与实际guest产物一致；
新增产物身份在整体实验窗口初末核验，未将其写成每run各自核验。
原APK/libg/libfmod/probe/Provider/update保持原字节。私有资源、密钥、XML、
原始快照、trace和运行配置留在隔离目录，公开仓库只含源码和脱敏摘要。

## 顺序矩阵、提交和backup

原桥与两次替换各完成11项：both-empty、o-backfill、p-only、conflict、equal、
unicode-backfill、unicode-conflict、write-conflict、write-partial、write-same、write-empty。
原生读取、双store操作顺序、逻辑值、密文/XML、真实commit窗口一致；每次矩阵
真实commit(true)合计7，原XML逻辑条目恢复，非PN条目保持。

两次替换均无桥错误、退休成功，10个强引用保持到退休；每次13条PN事件。
ART显示requestBackup和BackupManager.dataChanged各5次正常返回，具名调用
同线程严格嵌套；仍有7种未命名方法、480条未命名记录，语义unknown。
backup方法返回不证明操作系统backup交付，交付仍unknown。

第二次替换有31次**非PN键**走原桥，第一次为0。计数按入口筛选分支累加，
并非PN替换失败，也不是全局配置fallback计数；未收集这些键名，来源语义unknown。
显式PN矩阵窗口内计数保持不变。首次验证器错误要求全程0，exit 1保留；修正后
验证真实计数和窗口稳定性，摘要保留31，不据此升级严格门。

## 测试、失败与资源

专项85 passed；E2回归271 passed / 171 skipped，均exit 0。skip为缺少其他历史
私有附件等显式跳过，不计作原生通过；集合有重叠，不累加。两份JS语法检查通过。
真实附件负测覆盖构建、输入、进程、源码、快照、资源调用、matrix、退休和回退窗口。
首轮负测84 passed / 1 failed：零事件证据中查找已有AAsset行导致fixture错误；
改为注入一条事件，后续验证器正确拒收，原始失败日志保留。

此前两次RPC命名错误exit 1；一次回调引用回收导致安装后访问异常exit 1。
改正camelCase RPC和强引用后重新运行独立进程，不覆盖失败输出。
限定验证exit 0；旧严格门按原manifest复验仍exit 2，不修改历史BLOCKED。

公开检查exit 0：主组794 passed / 27 skipped / 89.19s；affordability 23 passed，
selection-timeout 82 passed，position 15 passed / 3 skipped，live-command
25 passed / 4 skipped，dispatch 31 passed / 3 skipped，reservation
31 passed / 2 skipped，coordinates 38 passed / 3 skipped，collection-coordinator
65 passed。主工程211 passed / 21.23s / exit 0；gitleaks exit 0、no leaks found。
这些集合有重叠，不累加为独立用例数。新检查XML归档于本轮私有目录，历史公开XML
恢复原字节；不以重新生成的测试附件覆盖历史证据。

本轮独占实例与专用adb已回收，五个专用端口无listener；历史adb完整身份保持。
保留AVD目录、失败、源码与附件。未进入E2.2/no-VM、Android-free、训练或10×。

## 首个未解决依赖与下一步

首个缺口是原初始化中的PN读取/写入和密钥来源尚未由本桥接管；当前安装依赖
Java初始化完成，Cipher/XML writer仍是Java后端。46键可移除性仍unknown。
下一步继续E2.1：先对自然冷启动调用建立原后端契约和可退休安装窗口，再单独验证
冷启动接入；后续分别验证native持久化、写失败、并发、崩溃持久化和backup交付。
这些验收与targeted strict gate、no-VM、Android-free保持分开。
