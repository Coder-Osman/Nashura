# E2.1-N：历史异常恢复序列与store0正常字节（2026-10-28）

基线：`479280d9a17db0a059b9e424b57aab52fb16d0ba`。工作分支：
`codex/pure-linux-libg-headless-20261026`。本阶段commit由本文件Git历史及交付消息定位。

本轮实际推进配置前置依赖的两项：**store1原版padding异常后的返回字节**，以及
**store0使用历史原版身份的正常密码字节**。两项均在现有Ubuntu ARM64 guest运行，
各使用两份历史原版归档、每份两个新Bionic子进程。
这是历史参考的限定通过；**严格CATALOG_READY_PASS仍为false**。
本阶段未重新加载libg，ResourceManager保持BLOCKED，GameState及真实Step保持NOT_RUN。

## 实现与独立参考

新增8份源码和测试，修改2份既有工具。所有key、IV、设备身份原文、XML值及原始日志
只保留在私有隔离目录。公开证据使用相对归档标签、计数和SHA，不包含私有材料正文。

`preference_sequence_capsule.py`只从历史`direct_cipher`的**原版result字段**构建NCS1胶囊。
要求完整两store、固定顺序、明确的原版Cipher调用路径、原始ciphertext input及还原回执。
不使用stream/native输出作expected，也不通过数学重建补齐缺失密文。
解析XML前后快照并比较条目，同时独立保存原字节是否恢复。

`preference_sequence_probe.c`按单个持久解密上下文连续执行四条记录，检查返回码、长度、
逐字节输出及输出canary。失败输出长度必须为0；一条不匹配仍继续观察后续恢复，
整体退出2，绝不把局部恢复改写为全序列PASS。
`preference_sequence_job.py`复用双子进程和映射核验，仅接受固定有界结果及真实映射SHA。
通用reference job保留既有默认入口，通过显式validator复用收集流程。

`preference_historical_identity.py`要求历史归档明确记录来源为原版应用上下文的
Settings.Secure、初始化未替换、XML writer未替换、还原成功且cleanup_errors为空。
同时绑定当前真实DEX、dexdump、有界初始化图、constructor和IV来源SHA。
store0的o-key由历史记录的16位小写hex身份原字符串ASCII经SHA256派生，
不读取候选profile.keys或guest候选key。这修正了此前对历史身份输入缺失的过早判断，
**不代表Linux当前运行时身份获取已闭合**。

reference capsule新增显式`--store 0/1`，默认1保持旧入口；完整40行历史矩阵仍核验，
正常路径expected只来自original字段和已知明文。store0每份归档选择10个输入，
KEY、ENCRYPT、DECRYPT各一次，共30条；字符及长度范围沿用上一阶段，未扩大到Java全域。

## 实际运行与保留的失败

| 实验 | 实际结果 | 退出码与边界 |
|---|---|---|
| v106 首次胶囊 | `original-direct-cipher`缺少原始input，最初触发KeyError | 失败目录保留；后来改为明确ValueError，仍拒收 |
| v106 `original-stream-diff-3` | 两个新Bionic子进程，四条全匹配、canary完整 | wrapper0、child0/0，187输入未变 |
| v106 `original-stream-diff-4` | 独立归档重复，四条全匹配、repeat_equal=true | wrapper0、child0/0，187输入未变 |
| store选择初测 | 新测试发现循环局部store覆盖显式参数 | 首次1 failed/95 passed保留；改为row_store后通过 |
| v107 `derivation-final-1` | 历史原版身份绑定；两child各30条正常字节全匹配 | 提取/胶囊/build/wrapper及child均0 |
| v107 `derivation-final-2` | 第二历史归档相同限定对照 | 两child各30条、repeat_equal=true、187输入未变 |
| v108 隔离回归 | 专项378通过/1跳过、主工程211通过；公开九组1,104通过/42跳过 | 三入口exit0，集合不累加；248份历史XML与HEAD一致 |

v106原版和Bionic候选的四步序列均为：

```text
valid-before                返回16 bytes
bad-padding                 原版BadPaddingException；候选-3，输出0 bytes
valid-after-padding         返回32 bytes（包含持久失败后状态的影响）
valid-after-padding-again    返回16 bytes
```

这里比较的是已经记录的原版返回字节和失败后状态序列；C返回-3与Java异常类型的完整等价性
仍UNRESOLVED。异常输入全域、空解密、Java字符串及Base64语义也未由这四条证明。
宿主OpenSSL3在失败后的第三条返回32字节但内容与该Bionic参考不同；测试如实检查
探针报告不匹配、整体BLOCKED且继续恢复，未修改核心实现来隐藏后端差异。

两个v106归档均为`xml_bytes_restored=[true,false]`、`xml_entries_restored=true`。
store1序列化条目顺序发生变化，解析内容已恢复；**整份XML原字节恢复未通过**。
不得把该结果写成全部XML逐字节不变或原版持久化语义完全等价。

## 环境、映射与身份

guest为既有Ubuntu ARM64，`aarch64`、Linux `6.8.0-138-generic`。
各child实际映射9项：linker64、probe、[vdso]、libdl.so、libc.so、libnetd_client.so、
libc++.so、libm.so、libcrypto.so。除vdso显式无文件SHA外均核验真实路径SHA。
没有libg映射；187输入未变不代表187个文件均加载执行。
未启动Android/ART/UI，未变更VM配置、main、冻结任务或原始输入。

| 身份或输出 | SHA256 |
|---|---|
| 受保护libg（本阶段未加载） | `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783` |
| 当前DEX | `c781c71b622aaee4c0c1b94f8ae0c9aa639e228d66fb929a0c9c4672a5f81aa6` |
| dexdump可执行文件 | `9c4addfdb5373f0923e418a64683976922c1ffdd1a3e9673b9cce5a0838e9692` |
| 有界初始化图 | `a3953d78b813fbe1acf11cce103ddc80c89703e1f5831a3781fa5d18e5cce39d` |
| constructor摘要 | `ede1757207ae8739cf760276a6224c8ae18409cb1b8050bf95123268648e4827` |
| sequence probe | `bec1a6b3abc581ddefce31e6815c9fe7b9c464a957ebb126cb486293ea7f0399` |
| store0 reference probe | `ce5cd0e19c34ba23cefa68437f7af3775ec6262814ff50648d445cad125f73a3` |
| v106 diff3结果 | `794d464363925cd9995337da03c719e38ca49902a2db6bd54c616a780f9e572a` |
| v106 diff4结果 | `0af1a5aeafcfbabc3436c50d0e2731923171808c6bdf7f24a3c4ebf62bd67d3a` |
| v107 final1结果 | `518c2e23cbf490158adb791b53a3ebc5c6bf7da877a21f442d5cb264f9b6e11c` |
| v107 final2结果 | `d1e34d3cd2b2882fb00ed62e5392b386644d5849f33a2bfda19d9fae78ab1c82` |

构建、参考/observer/矩阵来源SHA、胶囊SHA、逐child stdout/stderr及映射库SHA见
[机器证据](pure-linux-preference-sequence-evidence-20261028.json)。v106共32项VM传输/命令操作、
v107共30项，操作回执退出码均0；传输成功本身不代替语义验收。

## 实际命令

以下仅替换私有绝对路径和材料为变量；完整argv、退出码及失败归档留本机。
输入材料须是SHA绑定的真实参考，每次输出目录独占。

```sh
"$NDK/aarch64-linux-android30-clang" -Wall -Wextra -Werror -fPIE -pie \
  tools/pure_linux/preference_cipher.c tools/pure_linux/preference_sequence_probe.c \
  -o "$SEQUENCE_PROBE"
# v106 build exit0；v107沿用reference probe并记录独立构建回执exit0

python3 tools/pure_linux/preference_sequence_capsule.py \
  --reference "$ORIGINAL_REFERENCE" --reference-sha256 "$REFERENCE_SHA" \
  --observer "$ORIGINAL_OBSERVER" --observer-sha256 "$OBSERVER_SHA" \
  --matrix-source "$MATRIX_SOURCE" --matrix-source-sha256 "$MATRIX_SHA" \
  --key "$PRIVATE_KEY" --key-sha256 "$KEY_SHA" \
  --iv "$PRIVATE_IV" --iv-sha256 "$IV_SHA" --output "$EXCLUSIVE_SEQUENCE_DIR"
# diff3/diff4 exit0；缺原始ciphertext的direct-cipher归档exit1、显式拒收

python3 "$EXPERIMENT/preference_sequence_job.py" \
  --payload "$PAYLOAD" --probe "$SEQUENCE_PROBE" --probe-sha256 "$SEQUENCE_PROBE_SHA" \
  --capsule "$PRIVATE_SEQUENCE" --capsule-sha256 "$SEQUENCE_SHA" \
  --expected-records 4 --output "$EXCLUSIVE_SEQUENCE_RESULT"
# 两个归档各wrapper0、child0/0

python3 tools/pure_linux/preference_historical_identity.py \
  --dex "$CURRENT_DEX" --dex-sha256 "$CURRENT_DEX_SHA" \
  --dexdump "$DEXDUMP" --dexdump-sha256 "$DEXDUMP_SHA" \
  --reference "$ORIGINAL_REFERENCE" --reference-sha256 "$REFERENCE_SHA" \
  --graph-sha256 "$GRAPH_SHA" --constructor-sha256 "$CONSTRUCTOR_SHA" \
  --iv-source-sha256 "$IV_SOURCE_SHA" --output "$EXCLUSIVE_IDENTITY_DIR"
# final1/final2各exit0；历史来源绑定，不是当前身份获取

python3 tools/pure_linux/preference_reference_capsule.py --store 0 \
  --reference "$ORIGINAL_REFERENCE" --reference-sha256 "$REFERENCE_SHA" \
  --observer "$ORIGINAL_OBSERVER" --observer-sha256 "$OBSERVER_SHA" \
  --matrix-source "$MATRIX_SOURCE" --matrix-source-sha256 "$MATRIX_SHA" \
  --texts-source "$TEXTS_SOURCE" --texts-source-sha256 "$TEXTS_SHA" \
  --key "$PRIVATE_O_KEY" --key-sha256 "$O_KEY_SHA" \
  --iv "$PRIVATE_IV" --iv-sha256 "$IV_SHA" --output "$EXCLUSIVE_REFERENCE_DIR"
# final1/final2各exit0；expected仅original及已知明文

python3 "$EXPERIMENT/preference_reference_job.py" \
  --payload "$PAYLOAD" --probe "$REFERENCE_PROBE" --probe-sha256 "$REFERENCE_PROBE_SHA" \
  --capsule "$PRIVATE_REFERENCE" --capsule-sha256 "$REFERENCE_SHA256" \
  --expected-records 30 --output "$EXCLUSIVE_REFERENCE_RESULT"
# 两个归档各wrapper0、child0/0，每child30条匹配

PYTHONDONTWRITEBYTECODE=1 python3.13 -m pytest -q -p no:cacheprovider \
  tools/pure_linux --junitxml="$EXCLUSIVE_TEST_DIR/pure-linux.xml"
python3.13 tools/run_main_engine_checks.py
python3.13 tools/run_public_checks.py
# v108独立工程副本，保留工作树历史XML；集合分别报告，不累加
```

## 当前门与下一步

| 节点 | 状态 |
|---|---|
| 真实libg加载、constructors、content/filesystem、provider读取 | 历史限定PASS，本阶段未重跑 |
| 历史store1四步padding恢复字节 | RUNTIME_OBSERVED / 限定PASS |
| 历史store0正常KEY/ENC/DEC字节 | RUNTIME_OBSERVED / 限定PASS |
| 当前身份、完整异常、Java UTF8/Base64 | UNRESOLVED |
| SharedPreferences分步commit、.bak、fsync、listener及BackupManager外部副作用 | UNRESOLVED |
| CATALOG_READY_PASS | false；自然ready历史记录仍保留 |
| ResourceManager | BLOCKED |
| GameState、空场/reset、真实Step、动作、多tick、snapshot、完整局和Android逐tick对照 | NOT_RUN |
| 无JavaVM生命周期、Android-free、10×及训练 | NOT_RUN |

首个未解决依赖仍为**当前配置平台契约**，并非guest、APK或dlopen缺失。
下一步绑定实际初始化身份和当前loading键调用链，闭合真实存储/commit/failure及backup，
再在两个新libg子进程重验严格catalog。已有历史正常与异常字节参考可复用，
不再把这些有界参考冒作整条配置契约。前置通过后才推进ResourceManager及GameState。
