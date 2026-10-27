# E2.1-N：真实原版库的原生初始化对象图

日期：2026-10-27。父提交：`2624b86f7b025d56c4abeb908996c7824448d1aa`。
分支：`codex/pure-linux-libg-headless-20261026`。本文件是新增证据，历史报告和 APK gate 保留。

## 结论与验收边界

真实 Ubuntu ARM64 进程已经从 LOAD_ONLY 推进到两个真实分配入口：

1. linker 日志实际记录 libg 的 **753 次 constructor 进入和753次返回**；
2. 内容 root/context 原生构造返回，两个对象非零、映射可读，**ready仍为0**；
3. 原生 filesystem 构造返回，生成392字节可读对象，但尚无 provider/catalog 初始化。

因此新增限定结果 `CONSTRUCTOR_TRACE_PASS`、`CONTENT_CONTEXT_PASS`。
整体仍为 **BLOCKED**，不登记 `MANAGER_INIT_PASS`。当前唯一前置阻塞是：
**资源 provider 注册参数与内容服务/表加载顺序尚未闭合，真实 catalog 未就绪。**
这不是已证明 JavaVM 必需，也不是引擎不可移植的证据。

没有调用 GameState constructor/setup/load、create_empty_battle、reset 或 GameStateStep。
没有完整战斗、无 JavaVM 冷启动、Android-free、10× 或训练结论。

机器可读的完整输入/输出 SHA、实际加载模块清单、命令及限定状态见
[证据](pure-linux-native-init-evidence-20261027.json)。原始地址、寄存器、内存和日志留在私有隔离目录。

## 固定输入和宿主

- Apple Silicon/macOS15.6 arm64 → 已获授权恢复的 Ubuntu22.04.5、Linux6.8.0-138-generic、aarch64。
- 使用既有 Fusion guest、gdb/strace；本轮没有启动 Emulator、安装系统组件或使用 QEMU。
- 用户 APK：15.535.13/versionCode150535013，SHA256
  `a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f`。
- 真实 ARM64 libg：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- 8个游戏库和9,829个assets/849,482,445 bytes沿用父提交的逐项匹配身份；
  runtime214、update990及fingerprint
  `e0cb2fb9eb2fdd0df1afcb00df3e3ba61cf25aa8b24e39c17cc391d952a4e339`保留。
- 本轮187个暂存输入（185个库、linker、既有ABI探针）逐项前后SHA一致；
  没有重新声称这些输入等于历史整包APK。历史APK SHA差异仍单列，supported_engine.json及旧gate原样保留。
- assets/update没有在本轮被注册、加载或运行验收；旧资源身份匹配不替代本轮资源语义证据。

## 实际运行与命令

所有 child 使用新目录，CORE=0；后续 runner 限时45秒、每文件最多32MiB，输出排他创建。
下列变量表示私有绝对路径，路径和凭据不提交。运行名与JSON中的原始记录一一对应。

```sh
# Apple Silicon宿主：现有NDK27.3.13750724 clang，Android24 AArch64目标
"$NDK/bin/aarch64-linux-android24-clang" -O0 -g -Wall -Wextra -Werror \
  -fPIE -pie -Wl,--export-dynamic tools/pure_linux/native_init_probe.c \
  -ldl -o "$PRIVATE/native-init-probe-v6"

# Ubuntu guest：脚本及elf_audit.py已经复制到本轮隔离目录
python3 "$RUN/native_init_job.py" --payload "$PAYLOAD" \
  --probe "$RUN/native-init-probe" --output "$RUN/baseline"
python3 "$RUN/native_init_job.py" --payload "$PAYLOAD" \
  --probe "$RUN/native-init-probe-v4" --content-root --output "$RUN/content-root-v4"
python3 "$RUN/native_init_job.py" --payload "$PAYLOAD" \
  --probe "$RUN/native-init-probe-v5" --content-root --filesystem --strace \
  --output "$RUN/filesystem-v5"
```

| 原生实验 | child exit | 观测结果 |
|---|---:|---|
| baseline | 0 | RTLD_NOW/LOCAL加载、PHDR、JNI符号解析、dlclose |
| debug | 0 | constructor/IFUNC完整linker日志，22.379秒；不用于吞吐 |
| strace | 0 | 独立LOAD_ONLY系统调用记录 |
| content-root-v3 | 23 | 原生入口已返回；观测器误拒绝Bionic tagged pointer |
| content-root-v4 | 0 | 仅修正maps范围比较，root/context可读、ready=0 |
| filesystem-v5 | 0 | 内容及filesystem原生构造返回，1.418秒含strace |
| boundaries-v6 | **inferior32/GDB0** | 入口断点撞到16字节指纹guard，计数全部无效 |
| boundaries-v7 | **inferior0/GDB0** | 两个正对照各1次，限定JNI/AAsset断点零命中 |

各实验的原始command argv、输出SHA及耗时已绑定在JSON。公开argv将两段入口原始指令字节
表示为verified-prologue占位；实际runner从匹配ELF提取并核对其SHA，不伪造指令或对象。
不将这些加载/调试时间解释为完整局吞吐。

## Constructors、映射与relocation

`dlopen(RTLD_NOW|RTLD_LOCAL)`真实返回，未遇到致命重定位错误。`dl_iterate_phdr`输出187项：
184个shared object、真实linker、自己的probe和kernel vdso；185个文件项均与输入SHA对应。
暂存的另两个输入是否映射见JSON列表，不能把185个候选库直接写成185个shared object。
`before.maps/after.maps/closed.maps`分别保留，dlclose成功也不证明所有库已卸载。

完整linker日志9,761,788行，SHA
`b94b3edbc0450af9ebf764de0298d995d9c94539e931eb62a3672590dbe119ff`。
88个模块实际constructor进入/返回各1070次，未匹配返回0、未返回0；libg753/753。
统计来自实际calling/done消息，未拿静态753槽替代运行覆盖。只证明linker调用及返回，
constructor内部JNI、间接调用和平台API仍存在观测缺口。

IFUNC实际304次、6个不同resolver；resolver归属和所有内部行为未闭合。
libc++_shared存在未知DT条目0x70000001警告，保留而不屏蔽；debug日志warning3条，
其他运行stderr存在重复打印，不把warning次数视作独立故障数。

## 对象图逐节点状态

| 对象图节点 | 状态 | 已知边界 |
|---|---|---|
| 真实libg | LOAD_ONLY_PASS | NOW/LOCAL及close真实返回 |
| constructors/实际映射 | CONSTRUCTOR_TRACE_PASS | 限定linker消息及文件身份 |
| content root/context | CONTENT_CONTEXT_PASS | 真实分配和连接；不含catalog ready |
| catalog/data tables | BLOCKED | 未建立真实provider与表加载序列 |
| resource manager | BLOCKED | filesystem分配子项成功，provider和catalog未运行 |
| battle manager/ReplayBattleController | NOT_RUN | 等待catalog前置条件 |
| GameState constructor/setup/load | NOT_RUN | 未创建GameState |
| empty battle/reset/单次step | NOT_RUN | 无前置状态，未调用getter冒充step |
| 原生对象destroy | NOT_RUN | 仅观察dlclose/进程退出，未单独验析构 |

content入口ELF偏移0x8001a4，root槽0x19ad878，context位于root+0x20。
filesystem入口0x12dfed4、global槽0x19f8d50；旧ProviderBootstrap0x12e157c是清空provider，
本轮没有误当作构造函数调用。

每次先核对真实libg SHA、入口16字节SHA和原slot为0，再调用无参原生入口。
没有传入自造对象，因此没有caller分配buffer的canary；用空slot、指纹和映射可读性guard。
content成功后记录root48字节/context2928字节FNV64摘要；filesystem记录392字节摘要。
摘要包含ASLR指针，仅用于该进程观测，不是跨进程语义状态比较或完整战斗快照。
v4只在maps范围比较时去掉指针top byte，不写引擎对象、不写ready。

## JNI路径与动态依赖

| 入口路径 | 本轮状态 | JNI边界 |
|---|---|---|
| A 直接dlopen | LOAD_ONLY_PASS | 本轮没有显式调用JNI_OnLoad，仅dlsym解析 |
| B System.loadLibrary | NOT_RUN | 没有从A推断该路径的注册行为 |
| C 显式JNI_OnLoad | NOT_RUN | 没有伪造VM或JNIEnv |
| D resident/serve_jni | NOT_RUN | 既有Android入口假定内容已ready，不是cold-start证据 |

v7在dlopen完成后的自有SIGTRAP marker暂停，核验module SHA后设置27个精确断点：
content/filesystem正对照、7个已知libg JNI/Asset桥、libandroid全部18个AAsset动态导出。
正对照置于entry+16以避开probe指纹guard，两者各命中1次；其余25个地址均为0。
这**只覆盖post-load两个分配步骤中的明确地址**，没有覆盖constructor内部、所有函数表调用、
动态resolver或别的JNI封装。零命中不代表JNI依赖不存在，也不代表已经无VM冷启动。
原始线程、LR、PC和x0–x3保留私有，仅公开计数和日志SHA。

filesystem-v5系统调用明确观测到：

- probe main前对`/dev/__properties__`进行2次stat、2次open，均ENOENT；这是loader/进程启动窗口，
  不能无调用栈地归因到libg。
- before_load与after_load之间尝试连接`/dev/socket/logdw`1次，ENOENT；精确caller未确认。
- 本轮没有clone/clone3，构造marker在主线程；不是所有生命周期都单线程的证明。
- 以assets、update:、data:、.csv搜索该syscall日志，匹配数见JSON；尚无资源注册/读取证据。

所以“加载成功”与“没有Android平台访问”是不同命题。这里已经观测到平台路径访问，
只是其缺失在当前限定步骤中未导致进程失败。具体依赖分类、未知项见JSON。

## 首个未解决依赖与静态时间盒

已有静态候选APKProviderRegister0x12e166c、DiskProviderRegister0x12e1714读取libc++ string形态参数，
分别绑定data:/update:；TablePreload0xe16a08需要表数组和虚对象，非无参入口。
ContentPostprocess0xdcd364只是wrapper，不能在没有表时调用或代写ready。
这些都是STATIC_FOUND，尚未登记运行所需的完整参数/初始化契约。

本轮仅对两个内容父函数及两个provider注册入口追加REA/Ghidra调查，startup时间盒240秒、
总deadline300秒。首次分析startup超时，其余请求取消，open/close及错误JSON全部保留。
没有扩大函数边界，没有拿旧伪代码冒充新成功分析，更没有据此盲目串接入口。

下一步唯一工作面：用匹配版本现有内容初始化调用点，闭合provider路径/string参数、
content service顺序及真实table输入，再在新子进程一次只推进一个入口。
先观察实际资源读取和catalog ready自然变化；通过后才调查battle manager。
首次JNI若出现，记录具体调用/参数；没有出现前不主动执行JNI_OnLoad。

## 失败、资源事故与证据保留

初次LD_DEBUG=3产生约1.557GB日志及失败临时归档，guest盘写满。完整26文件已归档回宿主，
逐成员流式SHA校验后，仅回收本轮已验证的巨型临时副本；旧任务未清理。
归档103,305,332 bytes，SHA
`5d67f28ee6a15c17952f07e3836cbda5ee0ef1c5a1091f86150d030ebe9a8e35`。
回收后guest可用2,037,497,856 bytes。宿主早期截断debug stderr不作完整证据，使用归档成员。
后续runner采用LD_DEBUG=2和上述限时/限额。v3、v6、超时和写盘失败证据全部保留。

## 工程验证与发布

新增C探针、排他runner、constructor日志解析器、post-load GDB观测脚本和13项独立合成测试。
全部pure_linux专项42项通过（含此前29项）；单测验证工具错误处理，不替代原生语义一致。
公开/主工程回归与脱敏结果、提交标识见[回执](CODEX_REPLY_2026-10-27_NATIVE_INIT.md)。
本轮不修改main、冻结部署、输入二进制及旧报告；不启动训练或性能放量。
