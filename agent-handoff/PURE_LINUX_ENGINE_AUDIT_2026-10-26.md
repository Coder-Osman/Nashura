# 纯 Linux 原版引擎审计（2026-10-26）

执行中；以同目录最终回执及机器可读证据为准。

## P0 已核对事实

- 起始发布仓库工作区干净，起始 HEAD/main/origin/main：`0012201830cfbfcbfeb3d9dccd91a855028da869`。
- 任务分支起点：`6ddf3e3f70a9712df45fac31fedc8121f3a67485`。仅在 `codex/pure-linux-libg-headless-20261026` 工作，main 保持原状。
- 引擎 SHA-256：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`，实际文件匹配；ELF64 little-endian AArch64，27,554,368 字节。
- APK 锁定版本 15.535.13 / versionCode 150535013 / arm64-v8a；锁定 SHA 为 `685c115ee432d5fd2d7be856a58ea0cb029c9c187aa9061f3cd250994dfbcf2a`。本地搜索的 10 个 APK 均不匹配；远端检查待完成。
- runtime 15.535.86；fingerprint.json 实际 SHA-256 `e0cb2fb9eb2fdd0df1afcb00df3e3ba61cf25aa8b24e39c17cc391d952a4e339` 与锁定一致。
- 资源身份须继续按 competitive manifest/转换记录核验。fingerprint 的下载 SHA-1 和已解码 SCDB 字节 SHA-1 不同，不将文件名相同视为原始下载字节一致。
- 本机 macOS ARM64；已连通独立 Linux x86_64 实验节点。节点不启动正式训练，实验不复用冻结任务端口/目录。

## 必须保留的区别

历史 2026-09-27 Linux resident 路径使用 QEMU、Bionic、ART/JNI 与 Android framework natives；其既有完整轨迹对照属于历史 Android 用户态结果，不满足本次“无 ART/Java VM/Android framework”的定义。

常规 llvm-readelf/llvm-nm/llvm-objdump 的符号输出为空，是缺节表引起的工具可见性问题。按 PT_DYNAMIC/PT_LOAD 解析可恢复动态符号；此前空输出不证明 JNI 或导入不存在。

视频核验：NOT_RUN。本次没有以旧 Android 录像证明纯 Linux 执行。

## 当前门

PURE_LINUX_NATIVE_ENGINE=BLOCKED；PERFORMANCE_10X=NOT_RUN。P0 原始 APK 缺口、P3 无 Android 生命周期、P5 对照与 P6 吞吐均尚未闭合。合成 smoke 与工程测试不提升这些门。

## 2026-10-27：Apple Silicon / E2.1-N 实际续验

以上段落为此前未提交草稿的历史记录；原始字节已在私有实验目录归档，
SHA-256 为 `6f7be503b9601591c13e8504871779a63985491a9e74f7cfb738724b00f6132b`。
本节及机器可读 `dependency-report.json` 是本轮结果；没有连接此前 Linux x86_64 节点。

### 环境与保护

- 起始本地 HEAD、origin tracking 和远端任务分支均为
  `82b94be5ae5ceb52f8cee0f5873ec3b8845bb573`；当前任务分支为
  `codex/pure-linux-libg-headless-20261026`。main 保持
  `0012201830cfbfcbfeb3d9dccd91a855028da869`，未合并其他分支。
- macOS 15.6 / 24G84、Darwin arm64、zsh、36 GiB 内存；首次检查可用磁盘
  170,791,288 KiB（约 162.9 GiB）。macOS 是分析/交叉构建宿主，不是 Android ELF 加载宿主。
- Fusion 已安装，`vmrun list` 退出 0，运行 VM 数为 0。已有 Ubuntu ARM VM
  配置为 2 vCPU / 6144 MiB / NAT，但存在挂起状态文件。仅只读查看，未启动、恢复、
  重建或改动它；未取得 live guest `uname -m`、联网或启动成功证据。
- 所检查位置未找到 UTM / Docker Desktop；PATH 和本轮检查的历史工件位置未找到
  QEMU user-mode。未在 macOS 执行 Linux ELF，未安装新系统组件。
- 发现其他实验已有 Android 进程；未附加、停止或重启，未启动 Android Emulator。
  本轮没有训练、自博弈放量、原版引擎加载或性能实验。

当前动态阶段直接阻塞为 **BLOCKED_ENVIRONMENT：缺少已运行且可验证的独立 ARM64 Linux guest 会话**。
这与战斗逻辑能否剥离无关；原 APK 身份另有缺口，列在输入项，不混成同一个问题。

### 真实输入核验

| 输入 | 实际结果 |
|---|---|
| libg.so | 锁定 SHA 完全匹配，27,554,368 bytes，AArch64 ELF64 |
| runtime fingerprint | 锁定 `e0cb2fb9…a4e339` 匹配，runtime 15.535.86 |
| runtime manifest | 214/214 文件 SHA 匹配 |
| resources manifest | 990/990 runtime-update 文件 SHA 匹配 |
| app manifest | 8/9 匹配；历史声明的 original.apk 缺失 |
| 本地两份 APK 候选 | 包名、15.535.13/code150535013 与嵌入 libg 匹配；整包 SHA 为 `f1ee6d61…d7dd9b`，与锁定原包不同 |

原包锁定 SHA 为 `685c115e…bcf2a`；历史 app manifest 的原包预期 SHA 则为
`01b375e8…4fcb`。保留来源冲突，未重新定义支持身份；APK 仍标记 **MISSING_INPUT**。
本轮只分析已经匹配的独立 libg 和 manifest 字节；APK 候选版本/嵌入库匹配不等于原包匹配。
990 文件核验也不代表原 APK 资源或原始加密下载已经全部核验。

### ELF、符号与依赖审计

实际使用已有 NDK `llvm-readelf -h -l -d -Ws`、`llvm-nm -D`、`llvm-objdump -T`，
并新增 `tools/pure_linux/elf_audit.py` 从 PT_DYNAMIC/PT_LOAD 与 GNU hash 恢复动态符号。
上述命令均退出 0；普通符号工具结果空或有限，不作为无 JNI 的证据。

纠正旧草稿的“缺节表”：实际 header 有 **5 个 section headers、12 个 program headers**，
但没有可供普通工具使用的动态符号节表。ET_DYN 共享库无 PT_INTERP 是正常格式；
Android 可执行探针的 interpreter 才是 `/system/bin/linker64`。

- 480 个动态符号槽；剔除空名后 473 个未定义符号，其中 8 个 weak。
- JNI_OnLoad 加 4 个 Java 导出；JavaVM/JNIEnv 的 vtable 调用未必表现为导入符号。
- 12 个 DT_NEEDED：libscid_sdk.so、liblog.so、libandroid.so、libEGL.so、libfmod.so、
  libfmodstudio.so、libdl.so、libsentry-android.so、libsentry.so、libm.so、libc++_shared.so、libc.so。
- 本地可静态找到直接依赖及递归候选；ABI 筛选后检查 185 个 AArch64 ELF。
  **不代表运行时 namespace、版本符号或 relocation 已通过**；dlopen/dlsym 可引入额外依赖。
- 初版把 libstatssocket.so 的 ARM32/ARM64 两份内容差异误记为歧义。新增 ABI 筛选后纠正，
  目前无同 ABI 多内容候选。旧 v2 报告保留；不据此伪造 linker 运行成功。
- 9 个 AAsset 导入、Android 日志/属性、线程/TLS、时钟、文件、网络和 EGL 引用均逐类记录；
  未解析间接调用及 GLES 动态解析保持 UNRESOLVED。未证明它们全部为战斗必需。

分类、完整 SHA、命令退出码和私有日志摘要见 `dependency-report.json`；原始导入审计、
二进制、资源、反汇编和机器配置不上传。

### 生命周期调查

关键纠正：**GameStateTick 是 8-byte tick 计数读取器，不是推进函数**；推进候选是
`GameStateStep`。现有 resident 的 serve_jni 显式 GetJavaVM，export 要求有效 JavaVM，
content runtime 检查只读取既有内容上下文，并未构造无 VM 冷启动。

构造需要上下文/内容，setup 有间接分派；load 需要 reader 和上下文；step 需要 manager、
队列、内容与有效虚分派目标。动作 append 转移所有权；snapshot 对象含指针，应比较序列化
payload，restore 还涉及 service/controller；reset/destroy 涉及 listener、池及全局生命周期。
详细对象链见 `DEPENDENCY_MAP.md`。旧静态闭包中的间接边不完整，不能据此断言热路径无 JNI。

### 实际工程实验与边界

- 新增 direct ARM64 Linux 静态 syscall smoke、Bionic pthread/TLS/clock 探针及身份守门 runner。
- 两个探针用已有 NDK 交叉构建成功（exit 0，`-Wall -Wextra -Werror`）；未执行 Linux ELF。
- 专项 15 项通过，包含 sectionless GNU/SysV ELF、坏指针/字符串、SHA、输出排他、ABI 筛选、
  错误宿主不执行以及逐项 smoke 结果校验。这些都是工程/合成测试。
- 在真实 Darwin arm64 宿主运行守门器，exit 2 / BLOCKED_ENVIRONMENT；没有运行探针。
- 隔离的起始 tracked snapshot 上，主工程 211 项通过；公开入口退出 0（主组 794 通过、
  27 跳过，其他组见回执）。新工具由独立专项覆盖；测试集合不相加。
- 初次归档解压因系统 Python 不支持 tarfile 的 filter 参数失败；原失败保留。
  新目录内先校验路径与链接，再解压 tracked archive，未覆盖原输出。

结论：工程准备 **IMPLEMENTED / UNIT_PASS**；guest smoke **BLOCKED**；
Bionic 运行、真实 libg 加载、GameState 生命周期、无 VM、原生一致性和 10× 均 **NOT_RUN**。
没有 PURE_LINUX_SMOKE_PASS、LOAD_ONLY 或原生完整局通过证据。视频核验仍为 NOT_RUN。
