# E2.1-N：配置文件原语与Android 30平台证据

基线：`506c7fd8551099815072ead66a6d5cb7d3298969`。工作分支：
`codex/pure-linux-libg-headless-20261026`。本阶段commit由Git历史及交付消息定位。

本阶段完成真实文件I/O、分步故障及进程异常退出恢复的独立实现，并离线审查本机
Android 30平台实现。**没有将新文件原语接入libg；严格CATALOG_READY_PASS仍为false。**
不把候选文件测试写成原版SharedPreferences、无VM初始化或战斗生命周期通过。

## 实现与真实平台发现

新增`preference_files.h/.c`：两个独立slot，已序列化bytes输入，上限64KiB。
使用独占目录、非阻塞flock、进程/线程所有权、regular/single-link与O_NOFOLLOW检查。
写入顺序为旧文件转bak、创建新文件、write、fsync、close、权限0660、清理bak；
加载时优先恢复bak。同步失败及bak清理失败分别记录，不混同于write失败。
observer仅用于明确的故障注入及观测；没有ready写入、配置默认值或初始化跳过。

原语仅对应调用方已确认needsWrite的文件分支，没有generation/no-op判定、异步queue或
内存map。权限设置失败及非普通文件等边界采取显式拒绝；这些保守分支与平台全域语义
尚有差异。输入已序列化，不包含XML解析/生成、cipher、Binder或backup调用。

`preference_files_probe.c`检查真实文件内容、canary、权限及fork/exec重启恢复；
`preference_files_job.py`在两个新ARM64/Bionic子进程中运行，核验真实映射及前后输入SHA。
合成XML内容仅测试文件字节，不冒充游戏资源或原版配置expected。

`platform_preference_audit.py`绑定本机既有system.img中framework DEX和dexdump SHA，
提取10个目标方法的指令/寄存器/异常表摘要及直接调用关系。完整dump仅留私有目录。
未启动Emulator、ART或系统服务，所有平台方法结论标记为**STATIC_FOUND**。

| 平台静态发现 | 对候选和验收的影响 |
|---|---|
| writeToFile调用FileUtils.sync但未使用boolean结果 | 候选记录sync_error，仍允许本次commit成功；尚无Android动态故障对照 |
| 成功写后删除bak的boolean结果未使用 | 候选记录backup_remove_error；下次load可能恢复旧bytes |
| write/XML异常分支清理新文件、保留bak | 独立测试覆盖相应文件故障和恢复；XML异常本身未执行 |
| loadFromDisk优先恢复bak | 测试已有bak、重复失败写与进程重启 |
| commitToMemory先于磁盘写入 | commit失败时内存map不自动回滚；本文件原语没有内存map |
| BackupManager经ServiceManager、IBackupManager代理访问服务 | 当前真实服务绑定、Binder调用和副作用仍UNRESOLVED；Linux无服务不等于backup成功 |

平台摘要还覆盖commit/notifyListeners/enqueueDiskWrite/createFileOutputStream。
listener、QueuedWork、generation、XML parser/writer、进程身份及服务的传递依赖未闭合。
未将“静态存在”写成“运行时必需”，也未将静态空服务分支当作当前运行证据。

## 实际实验与保留的失败

| 隔离实验 | 结果 | 边界 |
|---|---|---|
| v109首次两次提取 | 遇到后续vdex symlink显式拒收 | 失败目录保留；随后只提取所需两个jar，未修改源镜像 |
| v110宿主文件probe | clang/build0、probe0；18组/402项父进程检查 | macOS文件候选；不是Android对照 |
| v111第一轮Bionic | build/wrapper/两个child均0；每child18组/402项 | 旧结果保留 |
| v111增强轮Bionic | build/wrapper/两个child均0；每child19组/444项，repeat_equal=true | 增加最大容量全内容/canary、0660权限及已有bak后再次失败写 |
| v112审计初测 | 首次1失败/8通过，选中块前混入无关modified UTF8 | 原工具回显存在历史；未单独归档该首次回显，不虚构原日志SHA |
| v112修复及真实审计 | 9项测试通过；真实dexdump/audit退出0，10方法全部找到 | 从最近方法metadata边界截取，选中块仍严格UTF8；runtime NOT_RUN |

增强轮的19组包括两slot重启、6个故障点、5个进程退出点、成功提交后退出、恢复失败、
已有bak下再次失败、容量/输出guard、租约/owner、非普通及超限文件、非法目录/路径。
444是父进程的检查计数，内部另有fork/exec/crash子进程；不累加成游戏覆盖率。
进程退出实验不等于断电恢复；未验证目录fsync和持久介质故障。

增强轮各187项受保护输入前后SHA一致，实际映射8项：linker64、probe、[vdso]、
libdl.so、libc.so、libnetd_client.so、libc++.so、libm.so。
除vdso无文件SHA外，均核验实际映射文件SHA。没有加载libg、libcrypto或ART。
本轮新文件原语与既有loading_config_hook分离，旧候选和旧失败证据保持原样。

## 实际命令与身份

完整argv、退出码及原始输出只留本机独占目录；以下用变量隐藏私有绝对路径。

```sh
clang -Wall -Wextra -Werror -pthread tools/pure_linux/preference_files.c \
  tools/pure_linux/preference_files_probe.c -o "$HOST_PROBE"
"$HOST_PROBE" "$EXCLUSIVE_CASES" -
# v110 build0/probe0；后续增强probe也由独立pytest编译执行

"$NDK/aarch64-linux-android30-clang" -Wall -Wextra -Werror -pthread -fPIE -pie \
  tools/pure_linux/preference_files.c tools/pure_linux/preference_files_probe.c \
  -o "$ARM64_PROBE"
python3 "$EXPERIMENT/preference_files_job.py" --payload "$PAYLOAD" \
  --probe "$ARM64_PROBE" --probe-sha256 "$PROBE_SHA" --output "$EXCLUSIVE_RESULT"
# 增强轮build0/wrapper0，两个child0/0

python3 tools/pure_linux/platform_preference_audit.py \
  --dex "$PLATFORM_DEX" --dex-sha256 "$PLATFORM_DEX_SHA" \
  --dexdump "$DEXDUMP" --dexdump-sha256 "$DEXDUMP_SHA" --output "$EXCLUSIVE_AUDIT"
# 真实audit0；仅离线STATIC_FOUND
```

system.img为既有Android 11/SDK30 ARM64镜像，前后SHA一致：
`b8a505ecfc13629aee537f7fa9dcafa0d8d66802a06707f40dac69591e46f70f`。
framework.jar SHA：`c17a2b45ae03bad051528fc1a9487a5c320b43b2fd4bf11c9b615cfda2ffe633`；
services.jar SHA：`c6992cdc2a3e23d5aeb6ab77e7a7f6889cfcf5c262c6ed86825e6fcbf601681d`。
平台DEX、工具、probe、逐child及输出SHA见[机器证据](pure-linux-preference-files-evidence-20261028.json)。

## 隔离回归与发布检查

v113在独立源码副本运行，三个入口退出码均为0：专项409通过/1条件跳过，
主工程211通过，公开九组1,104通过/42跳过；集合分别记录，不累加。
实际命令、输出SHA和逐组XML计数保存在机器证据的regression字段。
工作树248份已跟踪历史XML与HEAD逐字节一致，未用本轮结果覆盖旧证据。

## 验收状态与下一依赖

| 节点 | 状态 |
|---|---|
| 文件原语及独占job | IMPLEMENTED / UNIT_PASS / RUNTIME_OBSERVED；合成文件限定通过 |
| 10个真实平台方法 | STATIC_FOUND；原生语义动态对照NOT_RUN |
| Android文件、XML/map及完整异常等价 | UNRESOLVED |
| 当前初始化身份、BackupManager服务及外部副作用 | UNRESOLVED |
| 新文件原语接入真实libg | NOT_RUN |
| CATALOG_READY_PASS | false；历史候选自然ready不提升严格门 |
| ResourceManager | BLOCKED |
| GameState、空场/reset、真实Step、动作、多tick、snapshot、完整战斗、Android逐tick对照 | NOT_RUN |
| no-JavaVM生命周期、Android-free、10×、训练 | NOT_RUN |

首个依赖仍为**当前loading配置写入的平台契约**，不是guest/APK/dlopen缺失。
下一步需把已恢复的cipher和分步控制流接到真实XML/map、当前身份和明确backup服务契约，
先核验与参考的失败及重启行为，再接入真实libg并以两个新进程重验严格catalog。
未知服务、身份或输入必须显式失败，不能用空值、统一成功或伪ready跨过前置门。

独立回归、公开检查及交付SHA由[回执](CODEX_REPLY_2026-10-28_PREFERENCE_FILES.md)和机器证据记录。
