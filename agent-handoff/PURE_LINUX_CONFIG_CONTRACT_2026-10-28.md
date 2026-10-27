# E2.1-N：配置候选租约与来源初始化契约（2026-10-28）

基线提交：`237a448d824a5ab728b861f2993dd1d13932a5fb`。
工作分支：`codex/pure-linux-libg-headless-20261026`。本轮交付提交见同目录回执及 Git 历史。
整体状态 **BLOCKED**：候选配置尚未等价于实际 Android 配置契约。
本轮没有重新运行 libg catalog，也没有进入 ResourceManager 或 GameState。

## 已完成的真实实验

在现有 Ubuntu 22.04.5 ARM64 / Linux 6.8.0-138-generic guest 的两个新隔离目录，
分别运行 v81、v82。使用匹配 Android linker64/Bionic，运行的是本仓库的配置候选探针；
它们没有加载 libg。两次 runner 均 exit0，各11条进程回执：

| 用例 | 每轮数量 | 实际退出码 | 含义 |
|---|---:|---:|---|
| 新建并写入 | 1 | 0 | 候选状态写入成功 |
| 持锁期间同时恢复 | 5 | 各3 | 预期拒绝竞争者 |
| 正常释放 | 1 | 0 | 所属 holder 释放 |
| 释放后恢复 | 1 | 0 | 原值及计数恢复 |
| 杀死本轮 holder | 1 | -9 | 预期故障注入，失败记录保留 |
| 被杀后恢复 | 1 | 0 | 内核释放租约后恢复 |
| 未知键拒绝 | 1 | 0 | harness 确认 API 返回-1；不是给未知键默认值 |

各轮187个探针、linker和库文件的前后 SHA 一致。配置 state 的初末 SHA 均为
`dbbac21490473872663920a1f73354cd08b8325559d37ce6452b232cccc3de05`。
该187项集合不等于重新核验全部APK/assets/update；本轮未触及它们。

新增 `writer-lock` 的进程生命周期排他租约，使用 `flock`、普通文件及单链接校验；
init/finish/read/write使用同一mutex，已初始化上下文的fork继承被显式拒绝。
锁文件保持存在，不通过删锁绕过竞争。该机制约束本候选的多writer行为，
**不是 SharedPreferences 多进程语义已验证**，也不声称覆盖任意时刻的fork或外部文件替换。

## 静态初始化审计与新发现

新增可重放工具 `tools/pure_linux/preference_method_audit.py`。
DEX和dexdump均须指定SHA，工具重新执行真实dexdump，独占写入脱敏结果。
方法摘要保留寄存器、访问属性、输入输出大小、指令PC/符号/字面值及异常表；
忽略文件位置和符号池索引。数组载荷另从匹配DEX读取并校验结构、长度、SHA，
不把dexdump省略的数组内容当作已覆盖；尚未实现的switch载荷显式拒绝。

当前输入DEX：
`c781c71b622aaee4c0c1b94f8ae0c9aa639e228d66fb929a0c9c4672a5f81aa6`。
历史安装基线DEX：
`ef1ac708f6e836daa925afc50d3bf3b139213b3a72323a354cc2e9493b50f608`。
它们整体不同。8个配置方法的限定摘要相同：
配置对象构造、编码、读、写、get/store/removeKeyValue及requestBackup。

加入 `GameApp.onCreate(Bundle)` 后，9方法中8相同、1不同，**工具exit2**；
当前304条、历史310条归一化指令。历史多出库加载及native bootstrap probe调用，
保留该差异，不用8方法相同替代完整初始化等价。

进一步核对配置p对象赋值前的126条线性指令：
所引用数组载荷SHA相同
`57dd7055198a2f96d2a6c320c993c07b408824c25c177c72c11239af5016ce27`，
但前缀摘要仍不同。只读诊断将差异缩小到一条条件分支的目标偏移；
目标之后的控制流及传递依赖尚未闭合，因此保留 **UNRESOLVED**。
载荷相同不证明设备身份、package、派生密钥、运行时字段或外部调用相同。
旧v80/v83/v84审计和所有原始失败保留；v85补充数组字节覆盖，不覆写旧报告。

## 配置语义差异矩阵

以下原始行为为 **STATIC_FOUND**，候选行为仅为实现及限定测试证据；
不将历史PN_POPUP动态材料移作当前LOADING_SCREEN_STATE_LABEL的动态等价。

| 项目 | 原Android路径静态发现 | 当前候选 | 缺口 |
|---|---|---|---|
| 读优先级 | p非空优先，回退o后修复p并请求backup | 两槽优先级/修复及计数 | 外部对象初始化身份待闭合 |
| 键/值编码 | AES键编码及值编码，Base64转换 | 单键明文私有bin | 加密布局及派生输入未实现 |
| 空/损坏值 | 解密异常处理，空解密结果删除编码键并commit | 拒绝损坏私有bin | 清理/异常类型语义不同 |
| legacy键 | 读不读明文legacy；空写分别删除明文和编码键 | 无legacy布局 | 两次独立commit未表达 |
| 提交顺序 | o后p；各store独立commit，忽略返回boolean | 一次文件事务同时更新两槽 | 中间状态/失败/内存磁盘双状态不同 |
| null与空串 | null即使已空仍触发修改与backup | 保留计数差别 | 真实平台副作用未表达 |
| backup | new BackupManager后dataChanged，无catch | 本地递增计数 | 计数不等于实际请求或交付 |
| 并发 | 本轮没有新的Android运行时观察 | 生命周期flock、线程mutex | 候选独占，不等于平台并发模型 |
| 未知键 | 只对实际观测单键建立候选 | 显式失败 | 不扩张为全配置统一默认 |

## 实际命令与证据身份

下列变量是对实际私有路径的脱敏映射，不是自动发现输入的命令。
完整原命令、退出码和原始stdout/stderr留在独立私有审计目录。
`EXPERIMENT`=本轮新guest目录，`PAYLOAD`=已核验的既有运行时目录，
`CURRENT_DEX`/`HISTORICAL_DEX`=上述SHA对应输入，`AUDIT`=新输出。

```sh
aarch64-linux-android30-clang -Wall -Wextra -Werror -fPIE -pie -pthread \
  tools/pure_linux/loading_config.c tools/pure_linux/loading_config_probe.c -o "$PROBE"
# exit0，probe SHA f57def5a979c312d9322994e596e51415db291d00f75e7adb1dce4323e2195e9

python3 "$EXPERIMENT/config_lease_job.py" \
  --probe "$EXPERIMENT/probe" \
  --probe-sha256 f57def5a979c312d9322994e596e51415db291d00f75e7adb1dce4323e2195e9 \
  --linker "$PAYLOAD/linker64" \
  --linker-sha256 e6a2026a6252e0a80c242555e147faca98cd557e2adac1927cd5c096d70ce4c7 \
  --library-path "$PAYLOAD/libs" --output "$EXPERIMENT/result"
# v81、v82分别exit0，互不覆盖

python3 tools/pure_linux/preference_method_audit.py \
  --dexdump "$DEXDUMP" \
  --dexdump-sha256 9c4addfdb5373f0923e418a64683976922c1ffdd1a3e9673b9cce5a0838e9692 \
  --left "$CURRENT_DEX" \
  --left-sha256 c781c71b622aaee4c0c1b94f8ae0c9aa639e228d66fb929a0c9c4672a5f81aa6 \
  --right "$HISTORICAL_DEX" \
  --right-sha256 ef1ac708f6e836daa925afc50d3bf3b139213b3a72323a354cc2e9493b50f608 \
  --include-initializer --output "$AUDIT"
# v85 exit2，保留初始化不等价

python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit0，181 passed / 1平台条件skip
```

| 私有记录 | SHA256 |
|---|---|
| v81结果 | 4d9d0f7d157760a975e1ca79ede548f762a9e697ee0135ba39e1dea007cdc2e2 |
| v82结果 | 9d3b880aa91480543532557d350e805b916d90a8d6af3f636c01a3e27a36b858 |
| v85方法审计 | 6f211bff1a2026c522adfa9f02571e25565748befadcc57453dc7a09f8d85924 |
| v85命令回执 | de18e983c23f455a22a5cf0b0b22666f0fb78c38c7297063ef425dde3dd9bd7c |

全部进程回执、输入及日志摘要见
[pure-linux-config-contract-evidence-20261028.json](pure-linux-config-contract-evidence-20261028.json)。
独立公开/主工程回归和发布检查结果见本轮Codex回执。
原始反汇编、DEX、密钥、资源和运行路径不纳入公开快照。

## 严格门与下一步

| 节点 | 本轮状态 |
|---|---|
| 配置候选生命周期租约 | PASS（限定候选） |
| 当前/历史配置方法身份 | STATIC_FOUND，8/8相同 |
| 初始化与平台持久化/backup契约 | UNRESOLVED |
| CATALOG_READY_PASS | false；历史自然ready证据保留 |
| ResourceManager整体门 | BLOCKED |
| GameState/empty/reset/真实Step/动作/完整局 | NOT_RUN |
| Android逐tick对照、no-JavaVM生命周期、Android-free、10×、训练 | NOT_RUN |

下一步前置阻塞是配置契约：核实条件分支目标及传递初始化身份，恢复真实编码/
commit-failure的分步状态与backup调用契约，再在两个新子进程重新验收真实catalog。
不是缺APK或guest，不登记BLOCKED_MISSING_INPUT；不通过降低门槛推进GameState。
