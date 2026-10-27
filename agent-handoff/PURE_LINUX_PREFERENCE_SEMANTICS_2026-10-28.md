# E2.1-N：配置控制流恢复与平台语义边界（2026-10-28）

基线：`0a5f8fac037691c1587554a5c002275cd243e15a`。
分支：`codex/pure-linux-libg-headless-20261026`。本轮发布提交见 Git 历史及交付消息。
整体仍 **BLOCKED**：没有取得严格 `CATALOG_READY_PASS`。
本轮新增真实 DEX 静态审计、受限解释差分和配置候选的 ARM64/Bionic 实测；
没有重新执行 libg，没有接入 ResourceManager、GameState 或真实 GameStateStep。

## 初始化分支的有限闭合

前轮线性前缀比较中的分支位移差异，现通过实际目标、边和数组字节恢复有界图。
工具 `preference_initializer_audit.py` 从真实入口追踪到配置 p 对象赋值，
也保留可达的绕过及返回路径；不通过删除分支或忽略目标实现相同。
未知 switch、异常边、无效跳转、缺失边界和不支持的数组结构显式失败。

| 项目 | 当前及历史输入的实际结果 |
|---|---|
| 配置 helper | 8个方法的限定静态摘要相同 |
| 有界初始化图 | 142节点、15分支，两个输入一致 |
| 图终点 | initialized 1、return 1、throw 0 |
| 图 SHA256 | `a3953d78b813fbe1acf11cce103ddc80c89703e1f5831a3781fa5d18e5cce39d` |
| 数组 SHA256 | `57dd7055198a2f96d2a6c320c993c07b408824c25c177c72c11239af5016ce27` |
| 完整 onCreate | 仍不同，不把有界相同扩大为全初始化相同 |

v88及新目录v93各exit0。上述状态为 **STATIC_FOUND**，
不证明外部调用、运行时身份、package、后续初始化、XML或backup等价。
历史完整初始化审计exit2及旧报告保留。

## 分步配置契约候选

新增 `preference_contract.c/.h`，仅接受实际观测键 `LOADING_SCREEN_STATE_LABEL`。
codec、contains、get_string、edit、change、commit、decode_exception和request_backup
均为显式接口，缺失接口及未知键报错，不提供默认成功。
容量8192是候选约束，非已证明的原实现上限；C字符串接口也不表示Java字符串全域等价。

已恢复到候选的控制顺序：

- 读取p优先；p为空时读o，并单独修复p及请求backup。
- 写入先o后p；每slot单独读取、比较、修改、commit，不合并为一次事务。
- null与空串区分；legacy与encoded键分别删除，各自commit。
- commit返回false被忽略；候选后端独立表示内存已更新而磁盘未持久化。
- 解密异常和空解密触发encoded清理；getString类型异常在解密catch之外。
- edit先于encrypt；backup位于提交之后，失败保留此前副作用。

这些顺序由真实DEX控制流支持；实际密码、持久化和backup行为仍依赖尚待实现的后端。
旧 `loading_config.c` 未替换，这个新候选也没有接管原版libg配置桥。

## 真实DEX控制流上的有界差分

`preference_dex_vm.py`只解释四个限定真实方法：c.a.j、c.a.n、
ApplicationUtilBase.getKeyValue及storeKeyValue。
分支目标和异常区间使用实际指令边界；不支持的调用/opcode、歧义方法、
非法目标及超过8192步显式失败。
不启动ART；codec/storage/backup边界由C候选和解释器共用显式合成模型。

矩阵覆盖双store七类初态组合、读/null/空/同值/新值操作、
按事件注入异常及commit(false)。比较返回、调用顺序、内存/磁盘、commit计数和editor状态。

| 实验 | 用例 | 差异 | 退出码 | 含义 |
|---|---:|---:|---:|---|
| v89首次 | 4,522 | 252 | 2 | 保留失败，未作为通过 |
| v90当前DEX | 4,522 | 0 | 0 | 修正Python adapter的解密异常分类后 |
| v92历史DEX | 4,522 | 0 | 0 | 同一外部边界模型的独立输入 |
| v93当前DEX重验 | 4,522 | 0 | 0 | 新构建、新输出 |
| v93历史DEX重验 | 4,522 | 0 | 0 | 新构建、新输出 |

首次差异来自adapter把解密内部PlatformFailure转换成fatal，
而真实DEX在该位置进入catch。修正异常映射，保留解密外异常传播；C核心未因该修正改变。
四方法访问指令分别36/36、37/37、19/19、29/29。
这是访问指令覆盖，非全部路径、全部输入、真实Android或密码语义覆盖。
同一共享模型的零差异也不独立验证共享模型本身。

## Ubuntu ARM64/Bionic候选实测

v91在既有Ubuntu22.04.5/aarch64/Linux6.8.0-138-generic中运行两个新子进程。
匹配linker64/Bionic执行新编译C候选，均exit0、无timeout。
15个回调故障边界、8组场景断言通过；187项payload/probe身份前后不变。
Host构建、ARM64构建和host执行均exit0。

Probe SHA256：`100be4061e854c6d8457c3dec650810c9c403bd0a61382a25b67aef16280f0e1`。
两次stdout均明确 `scope=synthetic_backend_only`、`libg_loaded=false`。
本轮未记录该探针实际映射库的完整清单，不将187项payload当作实际加载覆盖率。
这些是真实guest中的候选运行证据，**不是原生游戏配置一致或catalog严格通过**。

## 历史PN证据的复用边界

只读审查另一分支K/L/M/N报告及实现，没有合并分支、复制私有材料或启动Android。
审查锚点：`4d4bfbd721ab3686db66777717fce49b9cee33d1`。
各报告SHA见机器证据。

关键发现是Cipher错误会保留跨调用状态：非整块错误可能持续影响后续有效输入；
坏padding之后也可能先返回不同长度数据再恢复。历史持久EVP仅成功final后重init，
既非每次失败reset，也非永久poison。
M的完整局在恢复存储但保留Cipher状态后执行；N的串行门仅限定通过，
没有证明通用并发等价。历史代码保留Java身份、store/XML、backup及VM。
这些PN_POPUP动态结果不直接证明当前loading键及Linux路径等价。

## 实际命令、身份与证据

变量仅脱敏原始路径；不是提供额外合成输入。完整命令、stdout/stderr及回执留在本机新隔离目录。
公开只保存源码、计数、SHA和状态；没有DEX、密钥、APK、二进制、资源或原始日志正文。

```sh
clang -shared -fPIC -Wall -Wextra -Werror \
  tools/pure_linux/preference_contract.c -o "$C_LIBRARY"
# v93 exit0

python3 tools/pure_linux/preference_initializer_audit.py \
  --dexdump "$DEXDUMP" --dexdump-sha256 "$DEXDUMP_SHA" \
  --left "$CURRENT_DEX" --left-sha256 "$CURRENT_SHA" \
  --right "$HISTORICAL_DEX" --right-sha256 "$HISTORICAL_SHA" \
  --output "$NEW_GRAPH_JSON"
# v88、v93各exit0；完整onCreate仍不同

python3 tools/pure_linux/preference_differential.py \
  --dex "$DEX" --dex-sha256 "$DEX_SHA" \
  --dexdump "$DEXDUMP" --dexdump-sha256 "$DEXDUMP_SHA" \
  --native-library "$C_LIBRARY" --native-library-sha256 "$C_LIBRARY_SHA" \
  --output "$NEW_DIFFERENTIAL_JSON"
# 当前和历史分别执行；v89 exit2，v90/v92/v93 exit0

aarch64-linux-android30-clang -Wall -Wextra -Werror -fPIE -pie \
  tools/pure_linux/preference_contract.c tools/pure_linux/preference_contract_probe.c \
  -o "$PROBE"
# v91 exit0

python3 "$EXPERIMENT/preference_contract_job.py" \
  --payload "$PAYLOAD" --probe "$PROBE" \
  --probe-sha256 100be4061e854c6d8457c3dec650810c9c403bd0a61382a25b67aef16280f0e1 \
  --output "$EXCLUSIVE_RESULT"
# v91两个child均exit0；未加载libg
```

| 输入 | SHA256 |
|---|---|
| 当前DEX | `c781c71b622aaee4c0c1b94f8ae0c9aa639e228d66fb929a0c9c4672a5f81aa6` |
| 历史DEX | `ef1ac708f6e836daa925afc50d3bf3b139213b3a72323a354cc2e9493b50f608` |
| dexdump | `9c4addfdb5373f0923e418a64683976922c1ffdd1a3e9673b9cce5a0838e9692` |
| linker64 | `e6a2026a6252e0a80c242555e147faca98cd557e2adac1927cd5c096d70ce4c7` |

每次输入、输出、命令回执及日志摘要见
[机器证据](pure-linux-preference-semantics-evidence-20261028.json)。
源码SHA、隔离回归和发布检查见机器证据及本轮Codex回执。

## 严格门与下一步

| 节点 | 状态 |
|---|---|
| 有界初始化拓扑与8方法比较 | STATIC_FOUND |
| 分步控制流候选、独立测试 | IMPLEMENTED / UNIT_PASS |
| 候选ARM64/Bionic运行 | RUNTIME_OBSERVED，限定PASS |
| 当前键密码、平台存储/backup及完整初始化 | UNRESOLVED |
| CATALOG_READY_PASS | false；历史自然ready不变成严格通过 |
| ResourceManager | BLOCKED |
| GameState、empty/reset、Step、动作、完整局及Android对照 | NOT_RUN |
| 无JavaVM生命周期、Android-free、10×及训练 | NOT_RUN |

首个前置阻塞仍是**配置平台契约**，不是缺APK或guest。
下一最小实验是为当前loading键绑定真实初始化身份及持久Cipher状态，
再独立验证存储的分步commit、错误及backup请求副作用。
有界C接口保留这些显式失败边界；在它们闭合前不向旧配置桥接入或调用下游GameState。
完成后才在两个新libg子进程重验严格catalog门。
