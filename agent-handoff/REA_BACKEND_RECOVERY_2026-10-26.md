# REA 后端恢复与原生战斗依赖闭包续报

日期：2026-10-26。任务分支：codex/headless-engine-plan-20261025。
基于4359eab7822f42edaba6e6588e01d676b54b2196；本轮没有修改main、原生引擎、冻结部署或训练准入。
此前调查保留为历史记录，本文件补充实际安装和反编译结果，不重写此前失败证据。

## 1. 结论与交付边界

**缺失反编译后端的问题已解决：REA/Ghidra已实际分析匹配指纹的ARM64 libg。**
不是只有安装成功或doctor通过：84个入口取得伪代码、完整函数汇编、调用者、被调用者及交叉引用；逐份校验源身份、地址映射、精确入口及直接BL边。

- 83/84的汇编地址序列与独立Capstone/unwind范围完全一致。
- TableLoadBatch合并了后继函数：原10条，Ghidra214条，额外204条。保留显式例外，不将合并签名视作原wrapper ABI。
- 另外两个REA未识别入口用独立Ghidra脚本取得伪代码；394条汇编和60条直接BL边均与原证据一致，但高层pcode越界，**仅供诊断，不计入84份通过档案**。
- 原始伪代码、汇编、完整调用图、分析数据库和86份本地Markdown档案均保留在私有证据区；公开仓库只有自有工具、合成单测和脱敏摘要。

**无UI历史完整局已证明；同时没有Java VM和Android框架的冷启动完整局仍未证明。**
状态保持VM_FRAMEWORK_FREE_INCONCLUSIVE。本轮新增原生对局0、训练0，没有修改或执行libg中的游戏逻辑。

## 2. 安装、版本和配置

### 2.1 已实际安装并使用

|组件|实际状态|边界|
|---|---|---|
|REA|6.5.0，CLI与程序化session实际使用|Codex MCP注册未修改；不宣称所有客户端已就绪|
|Ghidra|12.1.4 PUBLIC，官方release ZIP|下载SHA与GitHub release digest匹配|
|Java|现有Oracle JDK25，Mach-O arm64|满足本安装GettingStarted.md的Java21+要求，未安装另一套JDK|
|Native decompiler|官方随包源码本机构建，Mach-O arm64|同时构建sleigh/demangler/lzfse；非Rosetta x86替代|
|Hopper|6.6.0官方demo，签名及Apple notarization检查通过|未购买、未激活许可证，未关闭Gatekeeper|

Ghidra安装目录为用户Applications下的ghidra_12.1.4_PUBLIC；Hopper安装到Applications。
官方Ghidra包本次未含可用mac_arm_64反编译组件，实际执行support/gradle中的
gradlew --no-daemon --max-workers=4 buildNatives，14项任务成功，约29秒。
源码API核对采用本安装附带的Base-src.zip、SoftwareModeling-src.zip，版本与运行程序相同。

Ghidra下载身份：

- 官方仓库NationalSecurityAgency/ghidra，release Ghidra_12.1.4_build。
- 文件ghidra_12.1.4_PUBLIC_20260921.zip，569,649,598 bytes。
- SHA256：ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db。
- Hopper官方6.6.0 demo DMG SHA256：4e678442f4afe283566c34fa359f025b0e754065787caa0cae52fd7faf2f0a7b。

### 2.2 Provider readiness不是整体环境健康

最终doctor限定ghidra、hopper和实际样本：healthy=true，全部scope_checks通过。
**environment_healthy=false**：IDA/jadx/Kuna等非本任务后端缺失、skill身份及各客户端注册仍有提示。
本任务通过显式环境的REA CLI/session运行；不运行全局rea setup，不覆盖现有客户端配置。
环境只传给本任务子进程，并保存本地启动脚本；未修改shell profile、系统Java或Codex配置。

REA最新npm元数据在检查时为6.8.0，要求Node ^22.23.4、^24.22.1或>=26.12.1。
本机Node26.3.1适合当前REA6.5.0，但不满足该新版要求。已工作的真实Ghidra分析未出现必须升级才能解决的缺口，故本轮保留REA/Node版本。
未进行升级、未写REA注册配置，因此未执行升级配置迁移。以后升级先备份实际目标配置、审查diff并独立验证，不能直接运行覆盖式setup。
Codex配置本轮前后SHA一致；摘要保留本地，不公开其内容。

### 2.3 Hopper的真实限制

Hopper确实打开了样本，但本次仅报告6个segment、1个procedure、0个defined string；tick入口反编译返回No procedure exists。
因此只登记安装/readiness通过，**不登记Hopper目标分析通过**。尚未定位是导入、分析流程还是demo能力限制，不能直接归因许可证。
Ghidra已经满足主要分析需求，不继续购买或调整系统安全配置。

## 3. 样本、地址与证据可信度

契约仍为firstlight_CR/native_runner/supported_engine.json：
nullsroyale.rel.free 15.535.13，versionCode150535013，ABI arm64-v8a，运行内容版本15.535.86。
libg共27,554,368 bytes，SHA256为
110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783。
分析前后重算原文件SHA一致；只使用已存在的匹配样本，未上传第三方文件或私有审计。

### 3.1 避免成功分析错函数

本次Ghidra image base为0x100000；原ELF VA、文件偏移和分析地址是三个不同坐标。
例如tick原VA0x1062bc0，本次分析地址0x1162bc0。直接查询原VA曾成功返回另一个函数，这是保留的负例。
验证器从文件支持的LOAD范围推算offset，再使用匹配原始SHA的Ghidra source mapping换算，最后检查返回entry完全相等。
多映射、错SHA、邻近函数、缺失指令和未声明例外均失败。

REA的inspect_native_load_image对本ELF标记unsupported：该操作的自动验证范围不覆盖ELF；observations仍提供可用原始SHA和映射。
报告保留unsupported，不将observations当该操作整体verified。Ghidra分析内存的modified_sha与原SHA不同也不代表磁盘文件被改写。

### 3.2 覆盖与耗时

|运行|实际结果|时长|
|---|---|---|
|默认全库分析并取tick|真实反编译成功；发现地址误用负例|602.938秒|
|关闭全库Decompiler Switch Analysis|80/86入口|215.255秒|
|同配置，增加6个已知入口seed|84/86入口|207.267秒|

以上运行工作量不同，**不是严格配对性能A/B，也不是游戏吞吐**。
禁用的是全库switch恢复；定点反编译仍运行。跳表完整性不在本次通过范围内。

84份REA档案总计6,551条汇编、139,327伪代码字符、46,110条incoming reference、2,420条outgoing reference、184个未解析call记录。
引用包含PLT密集引用及非调用引用，不是独立业务调用数；184只属于所选84份档案，不能替代全图间接边数量。
84份所有已知直接BL边均存在；已导出的high-pcode操作未超出其声明函数body。

### 3.3 两个补验入口与边界问题

独立工具tools/ghidra/ExportBoundedFunctions.java使用-noanalysis、已核验unwind范围、全新分析库；不覆盖已有函数、不改二进制。

|入口|汇编/BL交叉核验|反编译限制|
|---|---|---|
|NativeReplayRunner|219条、32条BL均匹配|9个高层pcode操作落在body外，涉及2个地址；5条警告|
|ReaderRegister|175条、28条BL均匹配|2,308个高层pcode操作越界、192地址；3条警告|

反编译器出现Possible PIC construction / Changing call to branch，ReaderRegister的伪代码包含相邻区域。
这证明“强制声明函数+decompileCompleted”不够。ok只表示Ghidra返回伪代码，不是语义验收；high_pcode_outside_body必须同时检查。
两个入口的REA识别失败和补验警告都保留，尚未证明自动分析为何丢失seed。

## 4. 对依赖闭包的新结论

证据等级：静态发现、历史运行时观察、限定已验证、未知。下表均不构成无VM运行证明。

|发现|等级|影响|
|---|---|---|
|构造路径使用x0/x1/x2/x3/w4，分配对象后加载、step、tick、销毁调用可见|静态发现，指令交叉核验|候选入口得到实质反编译支撑，参数类型仍需runtime确认|
|tick读取manager+0x60；Step最终返回0|限定已验证：本版本静态指令|tick不是推进；Step返回值不是胜负|
|命令解析与原始快照使用X8隐藏sret|限定已验证：指令/高层参数storage|伪代码param_1未必对应C ABI的x0，禁止直接照抄签名|
|配置读取含getKeyValue及ApplicationUtilBase，写入含storeKeyValue|静态发现|配置冷热cache不能掩盖真实JNI依赖|
|APKAssetExists调用AAssetManager_open/close|静态发现，与原PLT交叉核验|资源依赖藏在provider虚表后，不能只看注册函数|
|ResourceLookup处理asset:/前缀，flag走不同注册表查询|静态发现|纯文件替代需覆盖namespace、优先级及错误语义|
|旧标签ProviderBootstrap对应shared_ptr释放、数组与注册树清理|静态发现；reset/cleanup角色为修正假设|不再按标签把它当启动器；原标签保留用于追溯|
|SnapshotServiceContext直接取service+0x48；bootstrap读GameApp+0x148|静态发现，原指令核验|原snapshot wrapper仍依赖全局服务；底层serializer应独立评估|
|update仍有world/queue/listener等间接分派|静态发现；动态落点未知|无直接JNI路径不等于热路径无平台依赖|

### 对最终问题的回答

目前证据支持**不启动Android客户端UI**完成原版局（历史独立实验），但仍没有同时去掉Java VM和Android框架的冷启动完整局证据。
最小未解决边界不是“找不到tick”，而是：

1. 内容冷启动：配置服务、provider/reader、规则表、ID/masks/map初始化的等价替代。
2. 资源namespace与懒加载：纯文件服务是否覆盖所有真实请求及覆盖关系。
3. 动态虚调用：动作、前中后段、终局、reset的实际目标是否触及平台API。
4. 快照服务上下文：原wrapper和底层serializer分开验证。
5. ELF/Bionic/libc++、753个init-array、TLS及依赖库启动：不是同为ARM64就可由macOS dyld直接运行。

## 5. 下一步最小实验（本轮未启动）

先在独立分析库修正两入口附近PLT/PIC及函数边界解释，目标是消除越界高层pcode，而不是手工修改游戏指令。
随后依照此前E0/E1/E2顺序，不并行移除多个因素：

- E0，最多2小时：既有独立基线按load/preinit、资源、new/load、动作/tick、终局、snapshot/reset分阶段记录JNI、AAsset及关键虚分派次数；固定工程轨迹核对原始完整状态。
- E1，最多2小时：保留VM，仅切资源provider，字节、namespace、seek/length/失败语义及完整局必须一致。
- E2，最多4小时：有界配置服务加原初始化顺序，验证全新进程无VM/ART/framework映射、无JNI调用，完整局与快照恢复一致；未知请求显式失败。

仅安装/分析授权不自动扩大为上述动态实验或引擎重写。本轮没有附加到冻结进程、重启服务器、停止其他任务或启动正式训练。
失败时保留首差异，训练准入门保持原状。

## 6. 自有工具、复现与测试

自有验证器：tools/verify_rea_evidence.py；合成测试：tools/tests/test_rea_evidence.py。
限定导出器：tools/ghidra/ExportBoundedFunctions.java，已由安装的Ghidra编译并真实运行。
公开工具不包含第三方反编译实现、原始汇编或私有路径。

~~~sh
python -m pytest -q -p no:cacheprovider tools/tests/test_rea_evidence.py \
  tools/tests/test_headless_native.py tools/tests/test_headless_trace_verifier.py

python tools/verify_rea_evidence.py \
  --static-root "$PRIVATE_STATIC" --export-root "$PRIVATE_REA_EXPORT" \
  --binary "$MATCHING_LIBG" --sha256 "$SUPPORTED_SHA256" \
  --allow-failed NativeReplayRunner --allow-failed ReaderRegister \
  --allow-extended TableLoadBatch

"$GHIDRA_INSTALL_DIR/support/analyzeHeadless" "$PRIVATE_PROJECTS" bounded-check \
  -import "$MATCHING_LIBG" -noanalysis -scriptPath tools/ghidra \
  -postScript ExportBoundedFunctions.java "$SUPPORTED_SHA256" "$ENTRIES_TSV" "$PRIVATE_OUTPUT"
~~~

TSV每行是本地标签、**已换算的分析地址**、字节范围，用tab分隔。输出目录使用新目录；工具拒绝覆盖同名输出及重叠函数。
Ghidra进程退出0不代替结果检查：必须检查输出存在、源SHA、decompile状态、汇编范围、直接边及high_pcode_outside_body。

- 新22项合成验证器测试与既有27项headless工具测试联合49通过，0.09秒；不替代游戏正确性。
- 原17项私有静态检查通过，0.202秒。
- 主工程211通过，19.57秒。
- 完整公开入口退出0：主组794通过/27跳过，46.60秒；其余组依次23、82、
  15/3跳过、25/4跳过、31/3跳过、31/2跳过、38/3跳过、65通过。
  跳过原因仍为缺私有附件，不冒充公开可复验；测试集合不相加。

本轮不报告新增原生吞吐、不改变G0/G1或34/43、不启动G2–G8。
