# E2.1-N：持久Cipher候选与历史原版字节对照（2026-10-28）

基线：`db684e816e9cad1f2ebb03cae1b89c9938f00b53`。分支：
`codex/pure-linux-libg-headless-20261026`。本阶段提交由本文件Git历史和交付消息定位。

新增17份实现及测试源码，在已有Ubuntu ARM64 guest执行独立密码候选，
两个新Bionic子进程各通过30条历史原版ECB/CBC正常路径参考。
**这是配置密码后端的限定进展，严格CATALOG_READY_PASS仍为false。**
本阶段未重新加载libg，未接入ResourceManager或GameState，未启动Android、ART、UI或训练。

## 实现与参考来源

`preference_cipher.c/.h`显式加载指定绝对路径libcrypto，严格解析EVP接口；
KEY使用AES-256-ECB，值加密和解密各有一个独立AES-256-CBC上下文。
仅成功final后重置对应上下文；失败保留EVP状态，不逐次重建、不永久poison。
重置失败则该上下文拒绝后续调用。失败不暴露部分输出，输出长度为0。
32字节key、16字节IV与输入长度均显式校验；明文上限4096、密文上限4112，
这些是候选边界，非已证明的原版上限。mutex和fork拒绝仅保护候选生命周期，
不代表原版并发语义。close须在调用方停止使用后执行。

`preference_identity_material.py`只从SHA绑定的真实DEX及审查过的有界初始化图
提取p身份变换：初始化槽顺序为o、c、p，p是第三项，非c。
使用真实24项signed-int32数组，对UTF16 code unit按原索引反向处理，再SHA256。
包名由私有输入显式给出；设备o身份、当前运行时包名获取、charset及locale仍未闭合。
key、IV、身份原文和XML值均保留在私有隔离目录，不发布。

`preference_reference_capsule.py`读取历史`pn-derivation-matrix.json`的**original字段**。
审查对应observer确认store1为p，codec-encode调用真实原版`c.a.h`及原持久Cipher，
不是使用旧native候选生成expected。工具核对参考、observer、矩阵源码、TEXTS源码、
key和IV的显式SHA，检查两store XML前后相同、还原成功、算法/IV/charset和40条完整记录。
从store1选择10个已知输入，产生KEY、ENCRYPT、DECRYPT共30条私有胶囊记录。
胶囊在0700新目录写入0600独占文件。参考归档内部一致不等于当前完整初始化已一致。

10个输入覆盖空串、1/15/16/17字节、中文、emoji、换行及XML特殊字符、255及4095字节。
DECRYPT比较原版密文字节解出的已知UTF8字节；本阶段没有实现Java字符串、Base64全域语义。
所选历史参考包含PN研究输入，但不等于当前LOADING_SCREEN_STATE_LABEL整条调用链验证。

## 实际实验与失败保留

所有实验使用新目录与所属子进程。私有目录简称v97至v105，完整命令与日志留本机；
公开机器证据只保存相对归档标签、计数、SHA及限定结果，不含原始日志正文。

| 实验 | 实际结果 | 退出码与边界 |
|---|---|---|
| v97 首次持久Cipher | 两child均正常退出，采集器误用逻辑linker路径求SHA | child0/0，collector1；失败保留 |
| v98 exit追踪 | strace确认输出693字节、exit_group(0) | 0；未核验187项前后身份，不当作身份门通过 |
| v99 映射修复 | 依据dlpi_phdr和/proc/self/maps定位实际文件，保留assertions及采集错误 | 两child0；187项未变、9映射验证 |
| v100 p身份提取 | 真实DEX静态变换与两条真实XML加密键参考 | CLI0；仅STATIC_FOUND，当前runtime绑定未闭合 |
| v101 primitives重建 | 两首块已知答案、20组边界/重置组合、两类失败序列 | 两child0；异常序列只是候选观测 |
| v102 XML键参考 | 每child两条真实历史KEY密文逐字节匹配 | 两child0；CBC明文尚未在该实验比较 |
| v103 正常路径参考 | 从历史original生成30条独立expected记录 | CLI0；native字段从未作为expected |
| v104 原版字节对照 | 两个新ARM64/Bionic子进程各30条全匹配，repeat_equal=true | wrapper0、child0/0、187项前后未变 |
| v105 隔离工程回归 | 专项304通过/1跳过，主工程211通过，公开九组零失败错误 | 三入口均0；fixture不替代原生语义 |

v101的非整块序列连续四次-3；坏padding序列为-3、成功但32字节不匹配、
成功16字节匹配、成功16字节匹配。记录实际现象，不硬编码成Android异常等价结论。
历史原版异常证据说明持久状态值得验证，但本阶段未对上述相同输入序列做原版直接差分。

## 实际映射与身份

v104实际映射：linker64、probe、[vdso]、libdl.so、libc.so、libnetd_client.so、
libc++.so、libm.so、libcrypto.so。文件映射均以实际路径核验SHA；vdso显式无文件SHA。
没有libg映射。187项payload未变是身份检查，不是187个库均加载或被执行。

| 关键身份 | SHA256 |
|---|---|
| 受保护libg输入（本阶段未加载） | `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783` |
| 当前DEX | `c781c71b622aaee4c0c1b94f8ae0c9aa639e228d66fb929a0c9c4672a5f81aa6` |
| 历史DEX | `ef1ac708f6e836daa925afc50d3bf3b139213b3a72323a354cc2e9493b50f608` |
| linker64 | `e6a2026a6252e0a80c242555e147faca98cd557e2adac1927cd5c096d70ce4c7` |
| libcrypto | `2f45ce72276ddde55e7b41afbca0ddd2b1ffd03f50e885d42ff323e0f3c82e5b` |
| v101 primitive probe | `49d42be3784a4322ba6c6fc975e872cf64f7528d55ca93300d8c59ab739fce6a` |
| v102/v104 reference probe | `ce5cd0e19c34ba23cefa68437f7af3775ec6262814ff50648d445cad125f73a3` |
| 历史original参考 | `6ea9940ea0796efce8ccc87346355873331c169dd0d936233d9629d485ef22d5` |
| v104结果 | `619e5fa51e2950d2a6999face5fb42f0931dbc1f752fdeed96b1b2f41843c7dd` |

guest为既有Ubuntu22.04.5、aarch64、Linux6.8.0-138-generic；
host工程回归使用已有Darwin ARM64/Python3.13，无安装、升级或VM配置变更。
每次child的stdout/stderr、真实映射库SHA、构建回执与参考来源摘要见
[机器证据](pure-linux-preference-cipher-evidence-20261028.json)。

## 实际命令

下列变量仅脱敏原始路径与私有材料；不是用占位数据替代实验。全部输出目录独占。
完整argv及退出码保存在对应私有归档，凭据未进入命令记录。

```sh
clang -shared -fPIC -Wall -Wextra -Werror -pthread \
  tools/pure_linux/preference_cipher.c -o "$HOST_LIBRARY"

"$NDK/aarch64-linux-android30-clang" -Wall -Wextra -Werror -fPIE -pie \
  tools/pure_linux/preference_cipher.c tools/pure_linux/preference_cipher_probe.c \
  -o "$PRIMITIVE_PROBE"
"$NDK/aarch64-linux-android30-clang" -Wall -Wextra -Werror -fPIE -pie \
  tools/pure_linux/preference_cipher.c tools/pure_linux/preference_cipher_record_probe.c \
  -o "$REFERENCE_PROBE"
# 构建均exit0；所有构建输出保留在隔离目录

python3 tools/pure_linux/preference_identity_material.py \
  --dex "$CURRENT_DEX" --dex-sha256 "$CURRENT_DEX_SHA" \
  --dexdump "$DEXDUMP" --dexdump-sha256 "$DEXDUMP_SHA" \
  --package-file "$PRIVATE_PACKAGE" --package-file-sha256 "$PACKAGE_FILE_SHA" \
  --graph-sha256 "$GRAPH_SHA" --constructor-sha256 "$CONSTRUCTOR_SHA" \
  --iv-source-sha256 "$IV_SOURCE_SHA" --output "$EXCLUSIVE_IDENTITY_DIR"
# v100 exit0；仅STATIC_FOUND

python3 tools/pure_linux/preference_reference_capsule.py \
  --reference "$ORIGINAL_REFERENCE" --reference-sha256 "$REFERENCE_SHA" \
  --observer "$ORIGINAL_OBSERVER" --observer-sha256 "$OBSERVER_SHA" \
  --matrix-source "$MATRIX_SOURCE" --matrix-source-sha256 "$MATRIX_SHA" \
  --texts-source "$TEXTS_SOURCE" --texts-source-sha256 "$TEXTS_SHA" \
  --key "$PRIVATE_KEY" --key-sha256 "$KEY_SHA" \
  --iv "$PRIVATE_IV" --iv-sha256 "$IV_SHA" --output "$EXCLUSIVE_CAPSULE_DIR"
# v103 exit0；只使用original及已知明文字节

python3 "$EXPERIMENT/preference_cipher_job.py" \
  --payload "$PAYLOAD" --probe "$PRIMITIVE_PROBE" --probe-sha256 "$PRIMITIVE_SHA" \
  --iv "$PRIVATE_IV" --iv-sha256 "$IV_SHA" --output "$EXCLUSIVE_PRIMITIVE_RESULT"
# v101 wrapper0、child0/0

python3 "$EXPERIMENT/preference_reference_job.py" \
  --payload "$PAYLOAD" --probe "$REFERENCE_PROBE" --probe-sha256 "$REFERENCE_PROBE_SHA" \
  --capsule "$PRIVATE_CAPSULE" --capsule-sha256 "$CAPSULE_SHA" \
  --expected-records 30 --output "$EXCLUSIVE_REFERENCE_RESULT"
# v104 wrapper0、child0/0；每child通过30条

python3.13 -m pytest -q -p no:cacheprovider tools/pure_linux
python3.13 tools/run_main_engine_checks.py
python3.13 tools/run_public_checks.py
# v105独立副本，各exit0，不覆盖工作树历史XML
```

## 未闭合语义与下一步

只读审查本机已有Android35 SDK源码，得到STATIC_FOUND：Conscrypt成功final后reset，
异常绕过reset；空解密且未update存在特殊分支。SharedPreferences的commit先更新内存，
再调度磁盘写、等待并返回结果；.bak、fsync、listener/main Looper及中断均有独立语义。
BackupManager通过ServiceManager查服务，存在服务时发送dataChanged(package)，并处理RemoteException。
这些SDK文件未被证明就是匹配运行时的实现，静态发现不升级为运行观察。
backup既不能统一当成功，也不能当作无副作用操作。

下一最小步骤是闭合当前配置平台契约：绑定实际初始化身份；在固定原版历史序列上
比较异常后状态、空解密及Java字符串边界；再逐项验证storage分步commit、错误恢复与backup。
若所需运行时证据尚缺，显式保持UNRESOLVED，不伪造默认值。
前置通过后才把后端接入配置桥，并用两个新libg子进程重验严格catalog。

| 对象图节点 | 状态 |
|---|---|
| 真实libg直接加载、constructors、root/context/filesystem | 历史限定PASS，本阶段未重跑 |
| provider注册、实际资源读取 | 历史限定PASS；不代表完整内容初始化通过 |
| 持久Cipher候选 | IMPLEMENTED / UNIT_PASS / RUNTIME_OBSERVED |
| 历史正常字节对照 | 限定PASS；两child各30条 |
| 当前身份、异常全域、storage与backup | UNRESOLVED |
| CATALOG_READY_PASS | false；历史自然ready不等于严格通过 |
| ResourceManager | BLOCKED |
| GameState、空场/reset、真实Step、动作、多tick、完整战斗及Android对照 | NOT_RUN |
| 无JavaVM生命周期、Android-free、10×和训练 | NOT_RUN |
