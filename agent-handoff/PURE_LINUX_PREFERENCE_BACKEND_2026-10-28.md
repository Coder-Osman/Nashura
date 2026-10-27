# E2.1-N：双槽配置持久化接入与 backup 边界

## 1. 身份与结论

分支 `codex/pure-linux-libg-headless-20261026`，基线
`3ec4e6f7790f6485ac328629ad1be038bba12ae4`。本阶段提交由本文件 Git 历史及交付回执定位。
实验使用既有 Ubuntu 22.04.5 ARM64 guest，`aarch64`、kernel `6.8.0-138-generic`，
既有 Android30 Bionic/linker64。未安装 Binder、ART、Emulator 或其他系统组件。

真实 `libg.so` SHA-256：
`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
187 项受保护输入前后身份一致；用户 APK 与历史整包 APK 的身份差异继续独立保留。
原始 APK、libg、libfmod、旧 probe、Provider、assets、update 的磁盘文件未修改。

**本轮推进到真实 libg 的 loading 配置双槽写入，但没有通过严格 catalog 门。**
候选在持久化后遇到未闭合 backup 契约，主动 exit96；不是原版 BackupManager 的动态失败。
独立 Binder 查询在返回前 SIGABRT，服务存在性仍 UNKNOWN/UNRESOLVED。

| 对象图节点 | 当前状态 | 本轮证据边界 |
|---|---|---|
| 真实 libg / constructors / content / filesystem | 历史限定 PASS | 历史证据保留，本轮不重验全部构造器 |
| data/update 注册 | RUNTIME_OBSERVED | 四个真实 libg 子进程注册及 preload 返回 |
| XML/string-map、历史 ASCII codec | IMPLEMENTED / UNIT_PASS / RUNTIME_OBSERVED | 限定类型、历史参考及文件持久化，不代表完整 Android 等价 |
| loading 配置双槽写入 | RUNTIME_OBSERVED | 两次独立批次、各两个 child，先完成两次 commit，再主动失败 |
| 当前初始化身份、完整异常、backup 副作用 | UNRESOLVED | 历史材料和 NDK 查询不闭合当前 Java 平台契约 |
| CATALOG_READY_PASS | BLOCKED / false | 四 child ready 仅观测到 0，表加载未完成，loader 未销毁 |
| ResourceManager | BLOCKED | 尚未满足上游严格门 |
| GameState / 空场 / reset / 真正 GameStateStep | NOT_RUN | 不越过前置门 |
| 动作 / 完整战斗 / Android 逐 tick 对照 | NOT_RUN | 没有原生战斗生命周期结论 |
| no-JavaVM 生命周期 / Android-free / 10× / 训练 | NOT_RUN | 未启动、未宣称 |

## 2. 实现

新增 `tools/pure_linux/preference_store.{h,c}`、`preference_codec.{h,c}`：
仅接受 string-map XML；拒绝非法 UTF-8/XML 与未支持类型；保留无关项。复用已有文件
原语、持久 cipher 和显式分步配置契约。codec 验证限定历史 ASCII 正常域；
XML 全类型、Java 字符串/Base64 全域、listener/queue 和异常传播仍未验证。

密码材料由私有输入提供，检查权限、regular/single-link 与 `O_NOFOLLOW`。
材料内容及材料 SHA 均不发布；公开命令的对应值仅用脱敏占位符表示。

新增 `preference_backend_probe.c`、`preference_backend_job.py` 和独立测试，
验证双槽写入、重新打开、历史参考及同值写入。该独立后端没有加载 libg。

新增 `preference_store_hook.c`，只绑定经版本入口字节 guard 验证的
`LOADING_SCREEN_STATE_LABEL` 写入桥。仅修改本轮子进程内映射，原始 libg 文件不变。
未知键、解码异常和 backup 未闭合均显式失败，不写 ready、不伪造 JavaVM。
`provider_job.py` 要求材料及两份 XML 与三个 SHA 完整配对，拒绝与旧简化候选混用，
只向独占实验目录复制 XML，并核验实验前后输入。`preference_hook_evidence.py`
独立解析真实输出，严格门不因写盘成功而放行。

新增 `backup_service_probe.c`、`backup_service_job.py` 和回归测试：
在独立有界 child 中调用真实 `AServiceManager_checkService("backup")`。
只有查询返回阶段齐全、退出码符合协议，才分类 SERVICE_ABSENT/FOUND；
abort、加载失败或缺阶段一律保留 UNRESOLVED，wrapper 不报告契约通过。

## 3. 实际原生结果

### v116：独立后端

build exit0，两个新 ARM64/Bionic child 均 exit0，每 child 48 条历史参考一致；
两槽各 12 项，只替换 1 个已有项，另 11 项保留，没有新增或删除。重新打开读取一致，
相同值不重复请求 backup。每 child 核验 9 个实际映射，187 输入不变，repeat_equal=true。

backup 在此仅为测试后端调用计数，不代表真实服务响应。未加载 libg，严格门仍 false。
两个输出 XML 的 SHA：

- `26679cbc054bd5ddfe6384f8816a6590ed145b9a0a2dbadf8dd829ae6a3a213d`
- `c4a53264bbd5b69b09bc93ffbda49985cdaca37c65a98e6d9e4a51520723f0e7`

### v117 / v118：真实 libg 接入

两批分别 build exit0，各运行两个新 child。四 child 均 exit96，四 wrapper 均 exit1/BLOCKED。
每 child 观测到同样顺序：data/update 注册 → preload 返回 → loading 配置桥 →
两槽各一次真实磁盘 commit → backup 未闭合 → 候选主动退出。

汇总为 `commits=2`、`disk_writes=2`、`false_commits=0`、`backup_requests=1`、
`backup_failures=1`、`decode_exceptions=0`、`entries=[12,12]`。
这里的 backup_failures 是候选 callback 返回 -1 的计数，不是原版 Java 服务错误计数。
每 child 的 188 个唯一实际映射均绑定身份，无 unresolved mapping。

输出 XML 各 1086 bytes，两批四 child 结果相同：

- store0：`2276663fa3137ba380bc6f69fde4c3a587f687792e17f4fa9047fe5337ab573d`
- store1：`447137ea8d385ebb697560a70aecfeff9a82a46f96b5be8adacfe948cd9dad94`

输入、probe、辅助文件与资源前后均未改变，postcheck_errors 为空。
本轮资源清单为 data/assets 619 项、update/search 各 990 项，
不是新增一次 9,829 assets 的全量 Android 字节对照。

### v120 / v121 / v123：真实服务发现边界

v120 只读环境检查未发现 Binder 节点、模块、sysfs 或挂载；三条命令 exit0/0/127。
Ubuntu 的 service 工具不等于 Android ServiceManager，进程列表也不证明服务查询结果。

v121 build exit0，两个 child 均在约 0.165 / 0.214 秒后 SIGABRT（returncode=-6），
没有超时。实际阶段仅到 `before_binder_load`、`after_binder_load`、
`before_check_service`；没有 `after_check_service`。真实 syscall 证据确认
`openat("/dev/binder", O_RDWR|O_CLOEXEC)` 返回 ENOENT，随后 SIGABRT。
因此查询没有返回，不能分类 SERVICE_ABSENT。两个 child 各 19 个实际映射核验，187 输入未变。

NDK checkService 不等于 Java ServiceManager.getService 或 BackupManager.dataChanged 的
完整语义。不能从本次 abort 推断原版 Java 路径必然 abort，也不能把未发现服务改成默认成功。
v123 只是只读取回六份已有 syscall 日志，每份核 SHA、六次复制 exit0，不是六个新实验。

probe SHA：`70a668b96eb60e4291691cf68275f8fa8bf9ebe042d9f0167e578cd5ac8fca73`。
首个 syscall 日志 SHA：
`22f20a653186832fcb51f0ca638d5be788ae34d7ead7d259c839e70b2d7c8cd6`；
第二 child 主日志 SHA：
`59d1c4075801d8e249b60284c0a179ec6b88f6a6222835447866c8843042495f`。
完整六份日志身份见机器证据，原始日志保留私有目录。

## 4. 实际命令与回归

以下变量仅脱敏替代已有路径，不是新增安装步骤。完整 argv、build sources、每 child
输入/输出 SHA、映射和日志引用保存在
`pure-linux-preference-backend-evidence-20261028.json`，不得把占位材料身份当作可公开输入。

```sh
# v116：两个独立 child；each exit0，未加载 libg
"$PAYLOAD/linker64" "$GUEST_EXPERIMENT/v116-preference-backend-5_uszg8b/probe" \
  "$PAYLOAD/libs/libcrypto.so" \
  "$GUEST_EXPERIMENT/v116-preference-backend-5_uszg8b/material.bin" \
  "$GUEST_EXPERIMENT/v116-preference-backend-5_uszg8b/references.bin" \
  "$GUEST_EXPERIMENT/v116-preference-backend-5_uszg8b/result/process-0/store"

# v121：实际调用，两个 child 均 -6；查询返回前中止
strace -ff -tt -s 128 -o "$EXCLUSIVE_OUTPUT/syscalls" \
  -e trace=openat,close,ioctl,exit_group \
  "$PAYLOAD/linker64" "$GUEST_EXPERIMENT/v121-backup-service-03k3ttq5/probe" \
  "$PAYLOAD/libs/libbinder_ndk.so"

# v122：在独立源码副本运行，三个入口均 exit0
"$PYTHON" -m pytest -q -p no:cacheprovider tools/pure_linux \
  --junitxml="$PRIVATE_EXPERIMENT/v122-public-regression-lcb4szym/pure-linux.xml"
"$PYTHON" tools/run_main_engine_checks.py
"$PYTHON" tools/run_public_checks.py
```

| 回归集合 | 结果 | 退出码 |
|---|---|---|
| pure_linux | 482 passed / 1 skipped | 0 |
| main_engine | 211 passed | 0 |
| public 九组 | 1,104 passed / 42 skipped | 0 |

三个集合不累加。pure-linux XML SHA：
`fdf1b6f43ec47457999ec5f2a0c731943ac8337f77c223459ddb934404c489c9`。
独立回归结果 SHA：
`5263ebcd70f4b1267425370bea27096bb66e2fb875d1b20dac7afc3cd4703292`。
执行过的 tools/pure_linux 源码逐字节等同本次发布源码；工作树 248 份 tracked 历史 XML
保留原字节。回归不证明 Android 全域语义或原生战斗成功。

保留 v115 collector 首次误要求新增 XML 键的失败：child exit0 不等于 collector 通过；
实际操作替换已有项，后续核验明确检查 12 项中仅 1 项变化。另保留两次官方 AOSP header
请求超时，不将网络失败视为动态语义证据。

## 5. 首个未闭合依赖与下一步

唯一首要阻塞是 **loading 配置写入的平台契约**：当前初始化身份、XML/异常完整域、
backup 副作用及异常传播。它有多个子项，不是简单缺一个 Binder 文件。

下一最小实验从已有 Android30 DEX 和历史原版轨迹恢复“无服务、查询异常、备份失败”
各分支的真实副作用与异常传播；按明确契约建立有界差分，再决定候选行为。
不得安装 Binder 来绕过证据缺口，不把 callback 默认成功，也不把历史 ASCII 材料
当作当前初始化身份已验证。只有严格配置门闭合后，才恢复 catalog → manager →
GameState → 空场/reset → 真实 GameStateStep 顺序。

库被映射不代表 API 被调用；未映射 libart 不代表已验证 no-VM 生命周期。
本轮没有完整对局、逐 tick Android 对照、性能或训练结论。
