# 原版战斗最小依赖闭包：第一轮原生逆向调查

日期：2026-10-26。范围：匹配版本本地libg只读静态调查、现有probe源码及既有运行证据交叉核对。

## 1. 结论与完成边界

**无Android客户端UI、无完整Emulator的原版完整局，已有独立历史运行证据；同时无Java VM、无Android框架的完整局，尚未证明。**

当前状态：VM_FRAMEWORK_FREE_INCONCLUSIVE。不是已证明不可分离，也不是已证明调用几个地址即可独立运行。
主要未解决依赖收敛为四组：

1. **内容冷启动**：建立内容根、规则表、ID、状态掩码、地图和后处理状态，而不经过Java配置读写与AAssetManager资源路径。
2. **间接分派**：真实动作、前中后段tick、终局、reset的虚调用实际落点。直接图看不到的API不能按“未使用”排除。
3. **快照上下文**：原始快照封装依赖全局服务；standalone包装路径不同，必须分别验证。
4. **装载与ABI**：ELF重定位、753个init-array项、Bionic/libc++、TLS/线程/分配器以及依赖库初始化。

优先实验应是对既有隔离基线按阶段计数JNI、资源API和关键虚表目标，然后切割资源与配置接口，而不是重写tick或给未知函数填空实现。
本次没有修改原生引擎、安装组件、启动新对局/训练或接管其他任务；只交付分析及下一步实验设计。

## 2. 身份、工具和证据分级

### 2.1 引擎身份

依据firstlight_CR/native_runner/supported_engine.json：

|项|结果|证据等级|
|---|---|---|
|包/版本|nullsroyale.rel.free，15.535.13，versionCode150535013|契约记录|
|ABI|arm64-v8a；本地ELF为AARCH64|已验证：静态身份|
|libg大小|27,554,368 bytes|已验证：本地字节|
|libg SHA256|110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783|已验证：与契约一致|
|APK SHA256|685c115ee432d5fd2d7be856a58ea0cb029c9c187aa9061f3cd250994dfbcf2a|契约/历史manifest，本轮未重算APK字节|
|资源|nr_15.535.13_release_ff1c6c29；runtime content15.535.86|契约记录，未重验整个资源树|
|runtime fingerprint|e0cb2fb9eb2fdd0df1afcb00df3e3ba61cf25aa8b24e39c17cc391d952a4e339|契约记录，不冒充本轮运行测量|

只读取上述SHA的libg；没有上传APK、库、资源或原始审计。所有地址是该版本相对VA。
ELF没有section table；使用LOAD segment映射VA到文件偏移，unwind元数据确定函数范围，不能将所有VA直接作为文件偏移。

### 2.2 实际工具覆盖

- **REA 6.5.0已实际调用**：返回provider_unavailable；Ghidra未配置/未发现安装，Hopper launcher缺失。诊断原文留本地。
- 没有安装分析组件，也没有声称生成了Ghidra/Hopper伪代码。
- 使用既有LIEF 0.16.4、Capstone 5.0.7提取指令、unwind范围、PLT/GOT、重定位、直接调用及全局访问。
- 71,280个unwind范围，加422个识别PLT stub；扫描4,336,660条解码记录。PLT和重叠范围不当作独立业务函数。
- 生成86份档案：完整汇编、直接调用者/被调用者、间接边、数据/重定位xref；50份人工摘要，36份低层调用骨架。人工摘要也保留未知，不宣称完整反编译。
- 全扫描44个范围有解码缺口，保留清单；86个选定入口无缺口，表列直接闭包无解码缺口或未映射直接目标。仍不是全程序覆盖。
- 本地JDK25 jni.h交叉核对：64位JavaVM表+0x20是AttachCurrentThread，不是GetEnv。

证据等级：**静态发现**=真实字节/源码结构；**运行时观察（历史）**=已有归档、不是本轮新运行；**已验证**=精确限定校验通过；**未知**=仍需反编译或动态实验。

## 3. 战斗入口与ABI证据

|职责|候选VA|参数/返回主要证据|限制|
|---|---|---|---|
|分配/释放|0x17e4b20 / 0x17e4b50|PLT指向_Znwm / _ZdlPv|C++分配器，不是GameState工厂|
|构造|0x1060ba8|X0=manager；W1=mode；X2/X3=context；W4=alternate|probe用0x230字节、mode1、alternate0，需已准备内容|
|标志设置|0x1060f84|W1写+0x1e8；world标志；alternate vslot0x50|保留虚调用条件|
|加载|0x1061b1c|X0–X3；JSON、seed、world、commands/events|不等于合法执行/终局已证明|
|推进|0x10620a4|内部0x1062274及tick helper0x115e058|W0最终为0，不是胜负或成功码|
|tick getter|0x1062bc0|读manager+0x60并返回|只读，不推进|
|JSON|0x121a648 / 0xd0e100|probe字符串/成员协议和调用点|所有权与错误路径需保留|
|命令构造|0xd2fe78|X0=json，X1=context，X8=sret|工厂后调用命令vslot0x60；每类语义未全还原|
|命令入队|0xd2eef0|X0=queue，X1=&command；vector/count写入|普通入队未清owner；另有mode2分支；入队不等于执行确认|
|快照封装|0xad9a50|X0=manager，X8=sret；0x90 wrapper|依赖全局service，详见第5节|
|序列化|0x10632b0 / 0x1062c3c|manager/stream/flags/context；header版本42|context语义与流虚方法未闭合|
|恢复|0x10634c0 / 0x106379c|manager/stream；body另有flag/context|需已有内容，不是进程镜像|
|快照释放|0x105e8bc|wrapper析构路径|只适用于匹配native对象|
|销毁|0x10610b0 → 0x988844 → delete|probe与多个native调用者顺序一致|先成员、后数组、再对象；存在虚析构|
|内容上下文|0x80031c / 0xeaf1c8|root+0x20 / 静态地址|getter不初始化|
|数据与地图|0xdd1340 / 0x857608 / 0xdf8508|ID查表/首次地图预热/getter|首次资源请求需单独计数|

现有probe的7组入口prologue与实际字节一致。语义以具体指令与调用约定为准，名称不是恢复的调试符号。

### 3.1 直接闭包规模不等于最小业务闭包

|根|直接可达地址数（含PLT）|含未知间接控制转移的函数数|
|---|---:|---:|
|构造|129|6|
|加载|1673|247|
|Step|2313|507|
|命令解析|277|4|
|命令入队|5|1|
|原始快照|328|165|
|恢复body|1224|276|
|销毁|218|37|

Step直接图含1,957个未解析间接控制转移点（BLR/BR等，可能含尾调用或跳表跳转，不全是虚函数调用）。图既包含当前mode可能不走的分支，也遗漏虚方法内的路径，因此不是完整闭包上界/下界，更不是“需要重写2313个函数”。
直接图未连到已识别的JavaVM helper或AAsset/EGL导入。**阴性结果不证明热路径无JNI/Android依赖。**

## 4. 启动依赖切割

现有可运行host源码链：ART注册framework natives → 真实AssetManager注册APK → System.load和原native生命周期 → 无UI GameApp/FileContext/Looper/preferences → AAssetManager_fromJava → 原filesystem/provider/reader → 配置/内容服务 → SCDB/逻辑表/状态掩码/ID/postprocess/地图 → GameState。
这里有源码证据和历史运行记录，本轮没有重跑。跳过Activity UI attachment不等于没有Java或framework。

### 4.1 内容根

静态确认0x8001a4在0x800230写全局0x19ad878，其+0x20来自0xdcc92c分配的0xb70字节对象。
0x80031c只是getter和断言，不会补齐启动。probe检查context+0x6e8为1；跳过步骤直接置ready位不等价。
原顺序：preload0xe16a08 → batch0xe1bdcc → masks0xc84370 → IDs0xdccee0 → postprocess0xdcd364。
原生内容根有独立入口，但同样规则语义的无VM整体冷启动仍未知。

### 4.2 Java配置路径

静态链：0x76cee4 → 0x76c274 → 0x13941b0 → 0x140c68c → 0x140c7fc → 0x17d8ae8 / 0x17d8be4 / 0x17d8e94。
它涉及配置构造、key存在性/读取、JNI环境/类/异常。0x17d8ae8从全局VM取表并调用+0x20。
JNI_OnLoad0x13cfe74经0x17d88c0在0x17d88d0写全局0x1a16b78，返回0x10006。
读路径有单项缓存；热读未触发JNI不表示冷启动/下个key也无需VM。写路径0x139429c → 0x140b7b4也需覆盖。
哪些配置实际影响战斗/加载分支尚待归因。最小替代是有界且经验证的配置契约，不是所有JNI返回0/空串。

### 4.3 AssetManager藏在provider虚表之后

0x1195a88将AAssetManager指针写入0x19f1298。APK provider构造vptr为0x18c3370，disk provider为0x18c3400。
APK表+0x38重定位到0x12e4508，明确调用AAssetManager_open、AAsset_close；其他方法还涉及read/seek/length/file descriptor。
只看注册函数直接图会漏掉这些边。现有disk provider值得试验，但尚未证明完整替代namespace、fallback、压缩/流和路径语义。
历史micro/nano记录称CSV回退仍必需，移除VFX出现快照差异；本轮未重做删除实验。平台剥离与资源裁剪应分步进行。

## 5. 热路径与快照

### 5.1 默认callback的限定静态结果

mode1/alternate0构造路径创建默认listener，vptr0x18ab290。
重定位：vslot0x2f0 → 0x983e90，vslot0x4b8 → 0x983f74，两者均只有RET。
这是特定类布局证据，不是实际load后动态类型证明。manager+0x218的alternate还可调用vslot0x18/0x20/0x50；当前路径先置null，不足以删除其他模式。
世界、队列、组件、命令和流对象其余虚分派仍待追踪。

### 5.2 原始快照包装额外依赖service

0xad9a50 → 0xc89c10 → 0xc8c5b8 → 0xc8c5c4：读全局0x19bd358后，直接读取service+0x48，没有null保护。
创建入口0xc8c85c还访问GameAppGetter的+0x148容器以注册服务。
现有standalone host走battle_probe_snapshot_wrapper，复用底层serializer，并在服务缺失时用诊断null context；它与原wrapper不是相同入口。
历史固定轨迹快照/恢复通过只证明那个harness范围。优先考虑验证底层serializer独立性，不为了原wrapper先恢复整个GameApp。
战斗快照不创建资源表、provider、VM、TLS或进程单例；快照优化与无框架冷启动是不同问题。

### 5.3 时间、线程与装载

构造/恢复直接图出现clock_gettime，Step出现time，postprocess出现pthread_create/self。可能属于未选分支、初始化、seed fallback或遥测；本轮没有动态次数证据。
DT_NEEDED实际为：libscid_sdk、liblog、libandroid、libEGL、libfmod、libfmodstudio、libdl、libsentry-android、libsentry、libm、libc++_shared、libc。
这些是装载依赖，不等于每tick执行。753项init-array已列出重定位，尚未逐项证明效果；原库及依赖库初始化都应纳入未来审计。
ARM64同架构不自动解决macOS的ELF/Bionic装载问题，本报告没有证明dyld可以直接加载该库。

## 6. 依赖矩阵

|依赖|启动|更新/动作/快照|当前判定|
|---|---|---|---|
|Activity/UI/Surface|历史host不启动|固定完整局已完成|限定可剥离，原库仍含UI代码|
|VM/JNI|配置/类/生命周期有明确链|直接图未连接已识别helper，间接未知|冷启动配置边界未闭合|
|AssetManager/framework资源|APK provider有明确依赖|首次地图/懒资源可能触发，未计数|纯文件提供待对照|
|SCDB/CSV/ID/masks|原有顺序明确|查表/构造持续使用|保留原字节与语义|
|快照service|原wrapper要求|每次原wrapper取context|底层路径单独验收|
|Bionic/libc++/TLS|分配/重定位/初始化|内存/锁/时钟/线程|零Android运行时未实现|
|EGL/FMOD/Sentry|装载/可能初始化|tick直接图未证明EGL/FMOD使用|既不能按DT_NEEDED认定热调用，也不能径直删除|
|系统属性/Looper/preferences|现有host使用|是否仅启动尚未计数|逐项切割，不泛化|

## 7. 下一步最小实验方案（本轮未执行）

所有实验在新的本机独立目录、既有且核验的环境中开展。无可用环境先报告缺口，不自动安装VM、Docker、反编译器或系统组件。冻结进程/端口/目录原样保留。

### E0：分阶段依赖计数（建议最多2小时）

- 使用已冻结工程轨迹，不新增训练seed；固定libg、资源、host、probe身份。
- 分别计数load、JNI/preinit、资源、new/load、首tick、前中后段、动作、终局、snapshot/restore、reset/destroy。
- 覆盖已识别JNI helpers、AAsset/provider方法、manager+0xb0/+0x218 vptr、命令vslot0x60及主要世界虚表；记录首次资源请求。
- 使用既有调试/观测方法先验证插桩不改变轨迹；不附加到冻结进程、不持久修改libg。
- 验收自然终局、完整动作回执、终局/队列状态及29边界完整状态一致；记录实际映射库。未覆盖虚分派单列。
- 这阶段仍保留VM，只证明依赖分布，不宣称VM-free。

### E1：保留VM，只切资源提供路径（最多2小时）

- 提供完全相同原字节，保留namespace优先级；利用已有disk provider/有界资源接口。
- 比对exists/read/seek/length/缺失资源/APK-update覆盖的字节与错误语义。
- 完整局、快照、reset一致且实际AAsset调用为0才通过；首个差异即停，不能靠默认值继续。
- 不同时移除配置或loader依赖，保持可归因。

### E2：无VM冷启动（最多4小时）

- 从E0得到真实配置需求，定义有界native配置服务；未知key及未覆盖写入显式失败。
- 按原顺序建立table/ID/masks/map/postprocess，核对数量、映射与就绪证据，不伪造ready。
- 全新进程从启动到终局无JavaVM、ART/libandroid_runtime映射及JNI调用。先用VM启动再隐藏它不算冷启动。
- new/load、合法动作、tick、自然终局、观测、快照恢复、reset/destroy与边界状态全部对照。
- 报告首个具体缺失条件，保留失败，不静默stub。

### E3：平台装载闭包及吞吐（后续独立阶段）

- E2通过后仍核对DT_NEEDED、init-array及Bionic/runtime依赖；“无Java”与“无Android框架共享库”分开。
- 确认ELF/ABI宿主后再设计窄装载接口，不预先大规模移植。
- 同硬件、同原字节、同轨迹重复A/B，报告端到端及初始化/step/capture/reset。静态扫描耗时不是游戏性能。
- 任一阶段失败停在当前阶段。即使工程通过，也不自动获得训练准入。

## 8. 实际验证、交付与限制

17项本地分析测试通过：SHA/ABI、非恒等VA映射、tick机器字、独立B/BL位解码、caller互反、PLT、CFG全局写点、sret、快照服务链、默认listener空实现、init-array及未知间接边保留。
这17项是分析证据一致性检查，不是17局原生运行，也未验证未还原语义。7组probe prologue匹配；REA失败原文保留。公共回归结果另见publication摘要。

本地私有battle-closure-20261026目录包含REA日志、源身份、分析器/测试、static-final证据、86份档案和FUNCTION_INDEX.md。
**完整高层反编译、36份骨架的完整语义及全部虚调用闭包仍未完成。** 不把工具受限后的低层结果包装成完全完成的REA反编译。公开仅发布脱敏报告/摘要，不发布原始汇编和私有审计。

任务基于9b3dfea8079af1eb8e243ea58db0ec38a9505dd3，位于codex/headless-engine-plan-20261025。
本轮fetch观察origin/main为0012201830cfbfcbfeb3d9dccd91a855028da869；另有独立pure-linux分支推进到82b94be5ae5ceb52f8cee0f5873ec3b8845bb573，另一个工作树有该任务未提交文件。本任务没有修改、合并或接管它们。

**最终回答：**原版战斗脱离客户端UI/完整模拟器已有证据；无UI、无JavaVM、无Android框架同时成立仍无完整局证明。最小待解决语义边界是**配置和内容资源的无VM冷启动**，之后还要闭合实际虚分派、快照上下文和装载初始化依赖。
