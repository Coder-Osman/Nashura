# E1续报：FMOD资源绑定与真实namespace对照

## Status Update：限定E1严格门通过

基于 `d49aab860ee8696808dcdf9118a30b55b3a2f5ff`，仅在
`codex/headless-engine-plan-20261025` 工作；提交身份以包含本报告的提交为准。
E0基线仍为 `74769af2c95fb095ae970a3b8c89934206805aea`。

**固定原生完整轨迹及规定资源矩阵的E1严格门通过，CLI退出0。**
这覆盖冷启动至原生终局、战斗对象销毁及最终reset的已配置观测边界。
正常生命周期真实平台AssetManager事件为0，没有按caller过滤或忽略FMOD。
Java VM、配置JNI和原内容初始化顺序保留；未进入E2，未证明无VM运行。
OS进程teardown、自然音频播放和未配置间接目标仍未覆盖，零事件不代表
所有代码路径均已穷尽。旧报告及旧BLOCKED回执作为历史证据保留。

## Todo：本轮完成项与边界

| 项目 | 证据等级与结果 |
|---|---|
| FMOD实际resolver、函数表及manager传播 | 运行时观察并验证，6/6资源槽接入同一Provider |
| 原生完整局 | 已验证，3602份快照、28动作、终局、restore/reset一致 |
| APK/update分派 | 已验证，24项真实引擎入口对照，不是仅测自有Provider |
| APK全量资源 | 已验证，9829项、849482445bytes逐字节一致 |
| 损坏ZIP及边界 | 已验证，合成归档经真实Android API对照；不是自然坏资源覆盖 |
| 两个E0短stub、其他间接目标 | 未覆盖或未知，未扩展边界补造计数 |
| 无Java VM、无Android、正式训练、10倍吞吐 | 未验证，本轮未执行 |

## 1. FMOD的实际资源链路

`e1_fmod_binding.js`同时观测实际libdl导入目标及FMOD内部resolver写回边界。
本轮直接观测到：FMOD调用libdl的dlopen加载libandroid，取得非零handle；
六次dlsym均使用该handle，返回对应的真实libandroid导出；resolver将结果
写入FMOD自己的函数表。仅在原目标及精确槽位核验成功后，将这六个资源槽
替换为已初始化Provider的对应导出，初始化结束再次读取保存目标。

六项为fromJava、open、read、seek、close和getLength。fromJava的实际目标
为Provider；返回manager与FMOD保存manager一致，传入Java global ref与
保存值一致。触发阶段为cold_ready_wait，初始化线程ID为22453。
计数没有删去caller或关闭FMOD，也没有更改原始ELF文件。

真实resolver调用2066条，无request_error；31条其他resolver请求保留为
unknown_resolver_request，不把它们解释为已验证的资源依赖或无关调用。
最终binding观测2113条事件、errors为空；完整局中自然FMOD文件open/read
未出现，因此自然音频资源播放仍未覆盖。

为验证被保存指针的实际使用，另经真实FMOD_OS_File_Open/Read/Seek/Close
入口诊断stored、compressed及missing资源。原版目标属于libandroid，替换后
属于Provider；两边字节、返回码、seek及EOF一致。两项资源分别8与1641bytes，
missing返回18，EOF返回16且读取量0，越界seek返回13。这是显式原生文件入口
实验，不宣称自然战斗音频播放覆盖。

## 2. 真实引擎namespace与fallback矩阵

通过libg自身filesystem resolve/open、APK/disk exists/open，以及返回文件
对象的虚函数执行读取、length、seek、tell和EOF。原版与替换各跑24项，
比较路径、字节、操作结果及实际分派事件。跨进程对象地址仅在资源分派记录中
映射为对应namespace；原生战斗快照没有字段归一化。

| 场景 | 实际原版行为；替换结果相同 |
|---|---|
| 仅APK | 普通路径和data:/命中APK；update:/缺失 |
| 仅update | 普通路径和update:/命中update；data:/缺失 |
| 两边同名、内容不同 | 普通路径优先update；显式data:/仍读取APK |
| 两边路径不同 | 按请求路径独立定位，不按basename混用 |
| 删除同名update fixture | 普通路径回退APK；update:/仍缺失 |
| 两边缺失 | resolve/exists/open的原版失败行为保持 |
| stored与compressed | 全量APK对照及原生文件操作均一致 |
| ./、../、双斜杠、未知namespace | 分别核对原版实际路径和失败行为，未统一伪造返回 |
| malformed.csv | 22bytes原样读取一致；不证明内容解析器接受该数据 |

每项保存24个文件操作观测值，覆盖EOF、越界及负数seek等。描述符/remaining
由全量Android资源API对照补验；namespace结论来自真实引擎入口，而非这些
API诊断。临时冲突文件按实验清单回收，原990个update文件清单前后相同。

## 3. Provider损坏语义修正与失败保留

原实现在open时解压并强制CRC检查，真实边界实验发现与Android不同。
修正为创建handle后按read需要materialize；stored资源CRC错误仍按Android
行为返回字节，输入身份继续以SHA核验。坏deflate的open成功、read失败，
包括零长度read返回-1；失败不推进游标，seek和close保持原版语义。

独立合成归档经真实Android AssetManager验证8种名称/数据情形、4种mode，
合计32项。两边open/read/重复read/remaining/6种seek/descriptor全字典一致。
该诊断故意触发48次Provider失败，全部计数保留，live_assets为0。
正常真实APK生命周期没有这些故意损坏输入，其failures为0；两类计数不混用。
旧followup-gate-v1退出2及此前失败运行全部保留，不覆盖成成功。

## 4. 最终原生证据及身份

replacement-v6：tick3601终局、28动作、3602份原生快照，总计9120440bytes，
与无插桩基线全字节一致。轨迹SHA256：
`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`。
restore后21tick分支摘要与长度一致，三个snapshot handle释放；原生终局与
最终reset响应逐项一致，最终tick0、snapshotHandles0、queuedCommands0。
GameStateDestroy、GameStateBaseDestroy、LogicCommandQueueDestroy均在
terminal_reset_destroy同线程scope真实观测，不以进程退出代替战斗销毁。

libg九槽全部核验；FMOD六槽全部接管。正常生命周期Provider操作19474，
failures0、live_assets0、entries9829；所有已配置真实平台AAsset观测事件0。
全量资源另档对照9829文件、849482445bytes、9829描述符和2项missing，
operations276073、failures0、live_assets0。该诊断故意调用原Android API，
不计入正常生命周期零调用回执，使用的Provider构建与完整局完全一致。

| 身份 | SHA256 |
|---|---|
| libg | `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783` |
| 离线包装APK | `f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b` |
| probe | `84cddf82e866540ff33d4b5f0c6b5b535c8485182e4cc84961028c7a56e65ec4` |
| libfmod | `299d2a6b03ec865cd9234f4f7484fb7e74928d0180dc6c4b455a8d2b5b54dd45` |
| 精确update manifest | `7a435be60fe6759509824194afa3748537c6cb4e945bdeaf7ae1bd3229cb7a82` |
| 990个update文件列表 | `411aa1c884c90366e4cae2600d13e886aebe000af282847eb494d65d7d62a942` |
| Provider二进制 | `82732f436560055e60a8f2e760b731bafd8436c02313cf086a109d55476f234e` |
| Provider源码 | `e074c24c4e7d8c852715bdc536b9541d528a206e194eef74e7bca5466e6d1e2a` |

APK/libg/probe/libfmod及990个update文件前后身份不变。包装APK不等于
supported_engine登记的原包，只声明其中libg精确匹配。私有附件不随仓库发布；
公开SHA索引绑定本轮实际证据，但离开本地附件不能单靠聚合重验原生结果。

## 5. 实际命令、退出码及独立测试

下列PY/CLANG是已有Python环境和Android API30 ARM64 NDK编译器；RUN为本轮
私有隔离目录，PROVIDER为上述最终SHA构建。run.py等本地驱动含私有部署信息，
不公开；公共探针及验证器随本提交交付。以下记录实际执行，不要求重启已回收实例。

```sh
"$CLANG" -std=c11 -Wall -Wextra -Werror -O2 -fPIC -shared \
  tools/e1_apk_provider.c -lz -o "$PROVIDER"
"$PY" "$RUN/run.py" replacement-v6 redirect "$PROVIDER"
"$PY" "$RUN/matrix.py" matrix-stock-v3 observed-v9
"$PY" "$RUN/matrix.py" matrix-replacement-v5 replacement-v5
"$PY" "$RUN/compare_stock.py" resources-stock-v4 observed-v9 "$PROVIDER"
# 均exit0；各运行证据分别保留。矩阵与最终局在独立尝试中完成。
"$PY" tools/verify_e1_gate.py \
  --comparison "$RUN/replacement-v6/comparison.json" \
  --counts "$RUN/replacement-v6/dependency-counts-final.json" \
  --redirect "$RUN/replacement-v6/redirect-receipt.json" \
  --semantics "$RUN/followup-gate-v2/semantics.json" \
  --output "$RUN/followup-gate-v2/gate.json"
# exit0；17项语义检查通过，输出独占创建，绑定24个附件SHA。
"$PY" "$RUN/mutate_gate.py"
# exit0；20个真实附件内存突变全部BLOCKED；另一次CLI篡改检验exit2。
"$PY" -m pytest -q -p no:cacheprovider \
  tools/tests/test_e1_gate.py tools/tests/test_e1_semantics.py \
  tools/tests/test_e1_engine_probe.py tools/tests/test_e1_resources.py \
  tools/tests/test_e0_observer.py tools/tests/test_e0_snapshot_verifier.py \
  --junitxml=publication/checks-e1-followup.xml
# exit0：96 passed in9.24s；工程fixture不替代上述真实原生证据。
"$PY" tools/run_main_engine_checks.py
# exit0：211 passed in58.52s。
"$PY" tools/run_public_checks.py
# exit0：主组794 passed/27 skipped in117.31s，其余八组均完成。
```

其余公开组为23、82、15/3skip、25/4skip、31/3skip、31/2skip、38/3skip、65。
集合有重叠，不累加。五个新增/修改观测JS均经node --check，退出0。
20个负向突变包括namespace内容/路由、FMOD真实dlsym/handle/manager、目标、
资源构建、损坏语义、update身份、restore/reset、平台事件、failure/live、
导入槽、原生destroy及counts SHA；全部拒绝，原始附件未改。

## 6. 隔离回收与Next Steps

本轮独立game、Frida、AVD及adb已核验身份后回收，所属监听残留0；原adb身份
和启动时间不变。克隆、成功/失败输出继续保留，没有删除历史数据或触碰冻结任务。
main、正式训练、G0/G1门及性能实验均保持原状。

本轮停止在E1。若后续继续扩充E1证据，应先覆盖自然音频请求、进程teardown
和未配置间接目标；这三类不从当前零计数推导。首个仍未解决的平台依赖是
原初始化/配置链中的Java VM及JNI，其移除属于E2，本轮没有实施。
