# E2.1-F：PN_POPUP 原桥优先级、冲突与回填差分

## 结论与范围

本阶段完成两个独立进程的真实原桥11项矩阵，并与已有独立原生候选比较。
p优先、o回填p、变化才提交在本矩阵内取得运行时证据；限定附件验证exit0。
G-E2.1严格门仍BLOCKED/exit2，PN原桥未native替换，46键全部保留，可移除性unknown。

起点`ccac50ec172d7f4626fa956cf75c6da594a55786`；阶段SHA以对应提交及最终回执为准。
仅更新`codex/headless-engine-plan-20261025`，main/origin/main保持
`0012201830cfbfcbfeb3d9dccd91a855028da869`。保留JavaVM、JNI和原初始化顺序。

矩阵在各自原版完整轨迹与reset之后执行；候选独立重放逻辑状态，未接管战斗桥。
完整轨迹非扰动与矩阵语义是两项分离的证据，不合并解释成native替换已通过。

## 实现与真实观测

- `E21PreferenceObserver.java`增加显式开启的有界PN命令；核对已分析的store类型及方法。
  仅保存和恢复两个store的PN明文键/编码键原raw条目，不清空其他配置。
- `e2_1_pn_matrix.py`固定11项输入，逐项保存原状态、原桥返回字节、真实commit窗口、
  后状态、独立候选receipt与磁盘逻辑值；finally执行原raw恢复。
- `e2_1_pn_matrix_candidate.js`将测试逻辑状态导入候选自有双文件；独占目录、close/free
  后直接读取文件，避免只验证候选的内存读回。自有格式不兼容原Cipher/XML。
- `run_e2_observed.py`只增加可选的post-trajectory矩阵路径，默认运行不做PN矩阵写入。
- `verify_e2_1f_pn_matrix.py`绑定14708个私有附件、构建/安装身份和最终源码；
  逐项验证真实原桥入口、JNI异常、UTF-8字节、commit返回与候选状态，不信任自报equal。

命令序列先保存原raw条目，设置o/p逻辑值，再从原native读写入口调用；配置JNI保持原样。
两次矩阵均无pending JNI exception，结果如下。下表描述关系，不发布配置明文。

| 场景 | 原桥结果/状态变化 | 窗口内真实commit |
|---|---|---|
| o/p均空 | 返回空，不回填 | 0 |
| 仅o非空 | 返回o，回填p | p一次true |
| 仅p非空 | 返回p | 0 |
| o/p冲突 | 返回p，o保持 | 0 |
| o/p相同 | 返回共同值 | 0 |
| BMP Unicode仅o非空 | 返回UTF-8字节，回填p | p一次true |
| BMP Unicode冲突 | 返回p字节 | 0 |
| 写入与双store均不同 | 两store更新 | o/p各一次true |
| 写入只与p不同 | 只更新p | p一次true |
| 写入与双store均相同 | 保持 | 0 |
| 非空双store写空 | 两store变空 | o/p各一次true |

每轮7读/4写；候选读值、close后磁盘逻辑值及changed/committed/failed/renamed等receipt
与本矩阵原桥对应。原write入口为void，不将残留寄存器解释成成功状态。
真实提交返回来自转发原SharedPreferences后端的观测窗口，序列、线程和丢失计数核验通过。
Java线程、Linux TID与ART ID不做无证据等同；本矩阵为顺序调用，不外推并发等价。

两轮结束各恢复两个store的原PN raw条目并逐项读回；观测器恢复原引用，退休自身
6个Java引用及1个JNI global reference。原生字符串probe生命周期仍保留既有未知边界。
backup_requested只表示候选意图，真实backup服务交付仍unknown。

## 原生完整轨迹与身份

最终runner的baseline-current、matrix-1和matrix-2使用三个独立原版进程：

| 项目 | 实际结果 |
|---|---|
| 原生快照 | 每次3602份，9120440bytes，全字节一致 |
| 动作与终局 | 28动作，tick3601原生终局 |
| 生命周期 | snapshot/restore/reset及17项控制回执一致 |
| 轨迹SHA256 | `0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5` |
| 资源路径 | libg九导入槽、FMOD六绑定通过既有校验 |
| 资源计数 | 实际平台AssetManager事件0；Provider失败0；live assets0 |
| 原始输入 | APK、libg、libfmod、probe、Provider及990个update文件身份保持 |

已有NDK API30 clang复编原生候选，与上阶段二进制字节一致，SHA256：
`d7000052d6b965364cbaced93b4aa59c916ceb29a011d21f9c1f10202264d6b5`。
Java observer用已有ARM64 JDK和SDK构建DEX，十条构建/安装/核验命令exit0。
安装后的自有库和DEX实际guest SHA匹配。原第三方文件保持不变。
私有二进制、快照、配置值、原始日志和实际路径仅本地留存。

## 失败记录与验证边界

首次限定验证因update清单末尾换行摘要与runner规则不一致而exit1；
修正为runner既有strip规则，仍逐次核验990行、退出码及前后摘要，不改变原始文件。
第二次限定验证因早期baseline的runner与最终矩阵runner不同而exit1；首次专项
1 failed/49 passed，失败原因相同。没有放宽源码身份检查，保留旧基线与失败附件，
以最终runner补跑独立baseline-current，最终限定验证exit0、专项50 passed。
旧严格门复核exit2按BLOCKED记录，不以本矩阵局部结果覆盖。

原始证据不公开；公开摘要不能单独重验原生结果。验证器缺附件时显式失败，
相关真实附件测试显式skip；路径越界、未绑定附件、SHA、symlink及证据突变均拒收。
宿主release fixture新增Android类仅用于编译；未用PN方法显式抛错，不冒充Android语义。

实例已核验身份后释放，专用端口listener为0，历史adb身份保持；保留AVD与失败证据。
冻结部署、训练后端、G0/G1目录和原始输入保持原样。测试命令与结果见Codex回执。

## 首个未解决依赖与下一步

首个缺口是PN原native桥集成与原Cipher/XML存储交接；当前只证明独立候选的逻辑矩阵。
下一步先明确保留VM阶段的存储交接、回退与backup交付契约，再在新独占实例内接入候选，
按冷启动、资源初始化、完整轨迹、自然PN调用、reset/restore分别验证。未知键显式失败。
并发等价、崩溃持久化、补充平面/非法编码/null输入与46键可移除性继续unknown。
本轮未进入E2.2/no-VM、Android-free、正式训练或10×实验。
