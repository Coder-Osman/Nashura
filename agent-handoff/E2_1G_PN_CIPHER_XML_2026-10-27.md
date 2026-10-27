# E2.1-G：原PN Cipher/XML格式兼容差分

## 结论与边界

在两个独立原版进程中，显式密钥native codec与原Java Cipher、原SharedPreferences
读写实现完成限定格式互操作。每次40组密文字节比较、20次原store读回及6项损坏
条目清理通过；11129附件限定验证器exit0。G-E2.1严格门仍BLOCKED/exit2。

起点`c3e5b3cd3bdd29bfb31cc579a2174254c59b1eb3`；本阶段SHA以对应提交和最终回执为准。
仅发布`codex/headless-engine-plan-20261025`；main/origin/main保持
`0012201830cfbfcbfeb3d9dccd91a855028da869`。JavaVM、原配置JNI和初始化顺序保持。

codec矩阵在原版完整轨迹和reset之后执行，未替换PN战斗桥或原XML writer。
完整轨迹对照证明本轮观测与原路径运行的一致性，不证明native替换后的战斗等价。
显式profile仅在私有实验目录提供；native代码未包含原密钥派生或配置身份发现。
本阶段既非no-VM，也非Android-free、训练准入或10倍加速证明。

## 实现

- `e2_1_pn_codec.c/.h`：显式绝对路径加载现有crypto provider，只接受32字节key、
  ECB或CBC模式及明确长度。CBC要求16字节IV，明文上限4095字节；未知模式、
  非法长度及padding错误显式失败。先在临时缓冲完成验证，失败不发布部分明文，
  临时缓冲清零。该codec不实现原密钥派生、XML writer或JNI桥。
- `e2_1_pn_codec.py`：同一C实现的宿主调用与严格Base64/UTF-8适配。
- `e2_1_pn_codec_probe.js`：已有ARM64运行环境内加载自有库，显式字节输入、
  最多256次调用、关闭与调用计数；不替换原版入口。
- `E21PreferenceObserver.java`及return observer：仅显式启用时提供有界原Cipher
  元数据/编码/读取/raw条目命令，要求先保存PN。原store和真实commit后端保留。
- `e2_1_pn_codec_matrix.py`及runner：完整轨迹后逐项比较，拉取真实XML；
  finally关闭codec并恢复原PN raw条目，失败证据独立保留。
- `verify_e2_1g_pn_codec.py`：绑定11129附件、14份源码、三进程身份、NDK复编与
  实际guest产物身份；重新执行宿主变换，拒收自报equal、缺绑定、路径越界、symlink、
  SHA变化、源码变化或越界结论。真实XML同时逐项检查非PN配置未变。

## 运行时结果

原store的键使用AES/ECB/PKCS5Padding，值使用AES/CBC/PKCS5Padding；
实际Java provider为AndroidOpenSSL，默认charset为UTF-8。上述名称来自真实Cipher
对象及字节差分，不以静态反编译替代运行证据。AES块长16时，两provider的padding
在本矩阵中逐字节对应，不外推其他算法或模式。

每个进程包含两个store、每store十种文本：空、1字节、15/16/17字节、中文、补充平面
字符、换行与XML特殊字符、255字节、4095字节。每种文本同时比较键和值两种模式：

| 项目 | 每个成功矩阵的实际结果 |
|---|---|
| 原Java/宿主C/guest C密文 | 40组逐字节一致 |
| guest native加解密 | 80次，反向还原UTF-8字节 |
| 原store读取native密文 | 20次一致，空值遵循原实现删除语义 |
| 真实XML | 密文/删除条目与原store一致，非PN条目保持 |
| 损坏密文 | 双store各bad-base64、短block、全零block，共6项 |
| 损坏清理 | 原实现读回空并删除条目，单store真实commit(true)各一次 |
| 恢复 | 双store原PN raw恢复，完整XML逻辑条目相等 |
| 观测器退休 | 恢复原store，退休自身6个Java引用及1个JNI global |

XML对照按逻辑条目比较，允许序列化顺序变化；不声称XML文件恢复到原始字节。
注入的是guest native产生的密文，写入XML仍经原SharedPreferences writer。
畸形数据清理是原实现行为，未实现或宣称native writer拥有同等异常/提交语义。

## 完整轨迹与输入身份

最终runner的baseline-current、codec-scoped-1和codec-scoped-2为三个独立进程。

| 项目 | 实际结果 |
|---|---|
| 快照 | 每进程3602份，9120440bytes，全字节一致 |
| 动作与终局 | 28动作，tick3601原生终局 |
| 生命周期 | snapshot/restore/reset和控制回执一致 |
| 轨迹SHA256 | `0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5` |
| 实际平台AssetManager事件 | 0 |
| Provider失败/live assets | 0/0 |
| 原输入 | APK、libg、libfmod、probe、Provider及990个update文件前后身份保持 |

已有NDK API30编译器复编codec与实际guest产物SHA一致；Java observer用已有JDK/SDK
构建，12条构建/安装/身份核验命令均exit0。宿主与guest crypto provider身份纳入
私有证据。未安装新组件、关闭安全保护或替换原始第三方文件。

公开仅白名单摘要。密钥、IV、应用作用域标识、XML、二进制、快照、原始日志和
私有审计只在本地保留。公开摘要本身不足以重验原生实验；缺真实附件的相关测试
显式skip，验证器显式失败，工程fixture不冒充Android行为。

## 保留的失败与修正

1. codec-1/2各exit1：显式APEX crypto路径受app linker namespace限制，open返回-2。
   之后使用同实例已有且可见的system crypto库；保留失败，不关闭namespace约束。
2. codec-system-1/2各exit1：shell取得的android_id与app作用域SSAID不同，派生profile
   被原键编码oracle拒收。修正为匹配独占实例app作用域的私有输入，再运行两个全新
   进程；codec-scoped-1/2均exit0。这不等于native密钥来源和派生链已经集成。
3. 回收检查首次exit1：历史ps记录采用strip，而新检查保留换行；失败发生在任何
   回收前。按原采集规则规范化后，完整PID/启动时间/argv一致才回收，失败记录保留。
4. 严格门复查exit2：PN替换及完整存储语义尚未通过；局部codec证据未覆盖原门。

## 首个未解决依赖与下一步

首个缺口是PN战斗桥集成、原XML writer交接及native密钥来源契约。当前显式profile
只证明格式兼容；下一步仍应保留VM，先定义最小PN持久化适配的初始化、密钥来源、
双store写入/回填、失败回退、backup及引用所有权，再在新独占实例内做自然冷启动、
完整轨迹和reset/restore对照。不得静默用默认值填补未知配置。

实际backup交付、并发等价、崩溃持久化、null/非法UTF-8输入及46键可移除性仍unknown。
未做46键移除反事实；没有将required解释成战斗必需。独占实例已核验后回收，
专用端口无listener，历史adb身份保持；保留全部失败和AVD目录。
未进入E2.2/no-VM、Android-free、正式训练、G0/G1或10倍吞吐实验。

实际命令、退出码与回归结果见同阶段Codex回执。
