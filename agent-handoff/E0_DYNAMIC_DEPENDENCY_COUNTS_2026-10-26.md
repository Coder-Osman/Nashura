# E0：Apple Silicon 本机原版战斗动态依赖计数

## 结论与范围

**已验证：本轮专用 Android ARM64 模拟器内，固定工程轨迹的插桩不变性通过。**
无插桩基线与两次成功插桩均原生终局，3,602份逐tick原生序列化快照全部逐字节一致，未归一化字段。
**运行时观察：JNI、JavaVM、AssetManager、配置桥及部分间接调用均实际触发。**
E0覆盖尚未完全闭合；不是无VM、无Android框架或依赖删除安全的证明。
E1/E2未执行，正式训练未启动，main、冻结任务及训练准入保持原样。
代码身份以本报告所属提交为准；实验基线为 `b3947d8720fa0645f78acf15f72546992da2d4b6`。

## 1. 宿主与输入资格

- Darwin/arm64，macOS15.6（24G84），36GiB RAM，原生zsh。
- 已有Android Emulator37.1.11、API30 Google APIs arm64-v8a；克隆历史AVD为本轮e0-only，2核/2GiB、独立userdata。
- 独立adb5039、emulator-5618；原adb5037及其进程不动。控制/Frida独立转发。
- 使用probe内部状态，不用截图识别或ADB触控验证战斗。
- 旧打包QEMU为Linux ELF/x86-64，与Darwin/Mach-O不兼容。Exec format error不是游戏引擎运行失败证据。本轮不要求另建Linux。
- 用户授权安装实例后才启动克隆；此前BLOCKED_BASELINE是当时无独立设备的状态，已解除。

| 输入 | SHA256 / 核验结果 |
|---|---|
| 原版ARM64 libg.so | `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`，27,554,368bytes |
| 本地合法历史离线包装APK | `f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b` |
| 历史reference probe | `84cddf82e866540ff33d4b5f0c6b5b535c8485182e4cc84961028c7a56e65ec4` |
| 精确资源树manifest | `7a435be60fe6759509824194afa3748537c6cb4e945bdeaf7ae1bd3229cb7a82` |
| 固定工程match fixture | `ab62e108532609de96b81e9e25408919e85128dc4e8ae6446f14372ed2fd55df` |
| 固定28动作fixture | `e5d1ad869faccd9863c5b0dd04064b3ecea5b04244b485b0acfecc952dedb3fe` |
| v3 observer | `bc15f64a34ae2c6703ae8e0492e2d2ceb8c2b400735bd170fc5a109690c93a4f` |
| 私有入口配置 | `5f7c86ad3fca0800216a1fcd25a08bb6fd703fe45e87e86c5b34db26e2f081c0` |

包版本15.535.13/versionCode150535013，与supported_engine版本/ABI一致。
**离线包装APK的SHA不同于supported_engine.json的原包SHA**；不声称APK原字节相同。
其来源与既有离线构建清单核验，提取libg与支持指纹相同。
只在本轮克隆内替换probe，不改原始libg、源AVD或历史部署。
资源精确核验990文件、151,493,511bytes；克隆旧多余资源改名保留后安装精确树。
实验结束再次复核libg、probe及990资源SHA均不变。私有原始输入不发布。

## 2. 方法、证据等级和复验

`tools/e0_dependency_observer.js` 使用既有Frida17.18.0，仅观测，不改参数/返回值或原库。
在目标独立engine0进程resume前安装module observer，捕获libg加载和JNI_OnLoad。
Step/Load观察reference probe的original trampoline，不覆盖旧inline hook。
配置入口、AAsset exports、VM/JNI表、静态provider表及运行时对象表按实际地址合并挂钩。
私有计数保留目标、候选别名、线程、最内层阶段、调用者、BL/BLR、首次序号及有限参数/返回样本。
公开只给聚合及指纹，不发布地址、原始汇编、资源请求或私有审计。

- 静态发现：候选函数名、表槽及边界假设。
- 运行时观察：目标进入、来源和次数。
- 已验证：固定轨迹终局、序列化状态一致、restore分支及输入身份一致。
- 未知：删除依赖后的状态影响、未捕获目标、完整平台状态和无VM冷启动。

同一实现多个label为别名，**不可跨label相加**。状态影响一律unknown；观测不是因果干预。

三次成功运行均返回：
`ok=true,error_code=0,start_tick=0,tick=3601,snapshots=3602,injected=28,queue_count=0,finalized=true,world_result_raw=1,stagnant=0`。
使用既有工程fixture，不增加训练seed。

- 两份插桩各对基线3,602份快照全字节一致，合计9,120,440bytes。
- 轨迹SHA：`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`。
- 第一份计数SHA：`89cbfecfa24c09e9402b5d45d408d5f911d5ad131fe3ea9b813e606a518b6778`。
- v3计数SHA：`69a68aed6c97ac1bae282d5c5ecf70ad7d0147846c6b57e28cbc574228cb4d86`。
- v3计数3,027行、378条hook coverage、2个hook失败。
- snapshot0→step21→snapshotA→restore0→step21→snapshotB：A/B均1,995bytes、digest `725cbfddd3b4541e`。释放3handles，reset后完整trace，终局再次reset。
- 最终tick0、snapshotHandles=0。原生序列化未覆盖的平台/全局状态仍未知。

阶段状态证据：load/preinit/resources时尚未创建可比较的战斗，保留cold-ready回执，
不填造战斗摘要。configure后generation1/tick0/stateEpoch1；分支restore后stateEpoch2；
reset后generation2/tick0/stateEpoch3；完整trace终局tick3601/finalized=true/队列0；
终局reset后generation3/tick0/stateEpoch4/finalized=false/handles0。
首tick、持续tick及动作覆盖在逐tick原生字节归档中，控制命令前后observe也保留；
未给每一次底层JNI调用单独创建状态快照，因此各依赖的状态影响仍未知。

## 3. 分阶段入口计数（v3）

“首次阶段”指首次被观测到的阶段；候选名字不代表语义已确认。以下所有依赖的状态影响均unknown。

| 候选入口/接口 | 次数 | 首次/阶段分布 | 来源分类 |
|---|---:|---|---|
| JNI_OnLoad / JavaVMBind | 各1 | load | libg加载/绑定链 |
| GamePreinit / GamePreinitConfigure | 各1 | preinit | 冷启动前置链 |
| ContentBootstrap / SCDBOpen | 各1 | resources | 内容初始化 |
| TableLoadBatch | 5 | resources | 资源表加载 |
| PreferenceExists | 108 | preinit32、load76 | 原生配置入口 |
| PreferenceRead | 18 | preinit5、load13 | 原生配置入口 |
| PreferenceWrite | 26 | preinit4、load22 | 原生配置入口 |
| PreferenceJNIReadBridge / NativeReadBridge | 各109 | preinit33、load76 | 配置JNI桥，别名不相加 |
| PreferenceNativeWriteBridge | 26 | preinit4、load22 | 配置写桥 |
| GameStateConstructor / Load.trampoline | 各3 | state_create / state_load | configure与两次reset |
| GameStateStep.trampoline | 3,643 | first3、early1,239、middle2,400、late1 | probe原始trampoline |
| GameStateSetup | 3,655 | 候选state_create标签 | 其中3,643来自UpdateInner，实际是热路径 |
| LogicCommandParse / Append | 各28 | action_parse / action_submit | 真实28动作 |
| command.slot12 | 26 | action_parse | 实际BLR目标 |
| command.slot0 / slot3 | 各28 | early10、middle18 | 实际对象方法 |
| StateSnapshot | 3,606 | snapshot | 原生序列化 |
| SerializeHeader / Body | 各3,610 | snapshot | 原生序列化 |
| DeserializeHeader / Body | 各1 | restore | 分支还原 |
| GameStateDestroy / BaseDestroy | 各2 | destroy | reset销毁链 |
| AAssetManager_fromJava | 2 | load | Java资源绑定 |
| AAssetManager_open | 2,751 | load2,749、resources1、state_create_load1 | 平台导出 |
| AAsset_read | 4,488 | load4,487、state_create_load1 | 平台导出 |
| AAsset_close | 2,467 | load2,465、resources1、state_create_load1 | 平台导出 |
| AAsset_getLength64 | 1,526 | load1,522、state_create_load4 | 平台导出 |
| AAsset_openFileDescriptor64 | 2,455 | load2,454、state_create_load1 | 平台导出 |
| AAsset_seek64 | 5,785 | load5,780、state_create_load5 | 平台导出 |
| VM.AttachCurrentThread | 9,639 | 首次load，full_trace4,450 | VM表目标 |

Step包含完整局3,601tick及两个21tick分支，共3,643。
early是进入Step时tick1–1199，middle为1200–3599，late为≥3600。

### 平台族按实际事件去重

| 宿主/最内层阶段 | JavaVM | JNI | AAsset |
|---|---:|---:|---:|
| load | 4,573 | 30,587 | 19,459 |
| preinit | 269 | 2,136 | 0 |
| resources | 0 | 0 | 2 |
| state_create_load | 79 | 274 | 13 |
| initial_state | 32 | 64 | 0 |
| snapshot_create | 139 | 391 | 0 |
| snapshot_branch | 43 | 116 | 0 |
| restore | 24 | 48 | 0 |
| snapshot_branch_replay | 52 | 104 | 0 |
| snapshot_release | 116 | 232 | 0 |
| reset | 36 | 72 | 0 |
| after_reset | 48 | 94 | 0 |
| full_trace | 4,452 | 9,060 | 0 |
| terminal | 28 | 56 | 0 |
| terminal_reset_destroy | 56 | 112 | 0 |
| final_reset_state | 44 | 88 | 0 |
| 合计 | 9,991 | 43,434 | 19,474 |
| 直接caller在libg内 | 9,630 | 18,569 | 19,473 |

0只表示该阶段已挂目标未记到事件，不是所有Android依赖为零。
full_trace平台调用主要来自并发Mainloop线程（VM/JNI合计13,322），另两线程70/105。
控制线程推进3,601tick，另一线程推进前置分支42tick。
并发线程未归入另一线程的Step栈，**不可由Step内计数为零推出tick无Android依赖**。

## 4. 覆盖缺口与失败证据

1. manager_b0.slot1和command.slot2短stub挂钩失败，count为unknown/null，不填0。
2. VM表8槽/5目标，JNI表233槽/229函数；APK provider18槽/16目标、disk16/16；挂到不代表调用过。
3. queue16槽/world24槽未捕获可执行vtable目标；manager_218未取到有效表。对象布局/间接调用覆盖未闭合。
4. command是shared_ptr参数，v3正确解引用后取到13槽；slot2仍失败。
5. GameStateSetup静态名误导：每tick调用。现配置的state_create标签最内层覆盖可能错分嵌套调用；下轮须修归类后再排除热路径依赖。本轮原始数据不改写。
6. 首次spawn gating未捕获secondary进程，超时中断exit130；明确spawn Engine0App后通过。
7. v2增加每hook原生ACCURATE回溯后冷启动SIGSEGV，runner exit1。关闭额外回溯的v3通过；根因未完全确认，失败记录保留。
8. 错用旧系统Python一次联合测试22 failed/47 passed，原因是既有hashlib.file_digest版本要求；Python3.13.5复验69 passed。未改无关旧代码。

## 5. 实际命令与结果

`$E0_RUN`为私有隔离目录，`$PY`为既有Python3.13.5。私有runner含匹配输入路径，不随仓库发布。

```sh
"$PY" "$E0_RUN/run_baseline.py"
# exit0，固定28动作原生完整终局
"$PY" "$E0_RUN/run_observed.py"
# exit0；v3改用独立输出目录重复同流程
"$PY" tools/verify_e0_snapshots.py \
  "$E0_RUN/unobserved-snapshots" "$E0_RUN/observer-v3/observed-snapshots" \
  --left-receipt "$E0_RUN/unobserved-trace.json" \
  --right-receipt "$E0_RUN/observer-v3/observed-trace.json" \
  --counts "$E0_RUN/observer-v3/dependency-counts.json" \
  --output "$E0_RUN/observer-v3/trajectory-strict-final.json"
# exit0，3602 snapshots passed，输出独占创建
"$PY" -m pytest -q -p no:cacheprovider \
  tools/tests/test_e0_snapshot_verifier.py tools/tests/test_rea_evidence.py \
  tools/tests/test_headless_native.py tools/tests/test_headless_trace_verifier.py
# exit0，69 passed in 0.17s，其中本轮E0 verifier 20项
node --check tools/e0_dependency_observer.js
# exit0
```

verifier要求完整终局字段、精确逐tick文件集合、空队列及全字节一致；失败hook记unknown，别名不相加。
无插桩trace0.51165s，首次插桩22.0347s，v3为25.1819s，仅反映观测开销；
不是剥离优化基准，不与旧QEMU异宿主吞吐混比，不声称10倍加速。

## 6. 首个未解决依赖与下一项最小实验

**尚未验证不运行Android客户端运行时、JavaVM和框架就能完成原版对局。**
最先需要处理实测资源provider的AssetManager完整语义，以及冷启动配置JNI的键/类型/写入契约；
关键间接目标及线程归因亦未闭合，删除依赖后的状态影响未知。

下一步应独立E1：保留VM和原内容初始化顺序，只替换资源provider；
核验namespace优先级、字节、exists/read/seek/length、缺失/回退语义，未知请求显式失败。
完整局、snapshot/restore、reset对照通过且实际AssetManager计数为零才判通过。
E2之后依据实测配置建立明确契约，未知键/写入显式失败，不统一返回0/空串或伪造ready。
本轮不自动启动E1/E2、训练或多机任务。

## 7. 回收与发布

核验PID、AVD名和server路径后，已停止本轮游戏、Frida、两条转发、e0-only和adb5039；
原adb5037进程保留，克隆磁盘及所有失败/成功原始记录留在私有目录。
公开仅工具、独立测试与脱敏结果。所有阶段完整原生状态证据均可本地追溯，不发布原始审计。
