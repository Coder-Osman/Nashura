# E2.1-N：真实 Backup journal 追加与 ARM64 Linux 局部对照

基线 `952cbd8c17b4cb8e926583d091055cde6efddc52`，分支
`codex/pure-linux-libg-headless-20261026`。交付身份为包含本报告的提交。

## 已取得的证据

本阶段结果为 **JOURNAL_APPEND_PARITY_LIMITED**。在原隔离 Android 30 ARM64
实例中，两个独立 `app_process64` 进程反射调用真实
`com.android.server.backup.DataChangedJournal`，观察创建、追加、readUTF/EOF、
异常以及另进程重开；退出码均为0。这里执行的是平台类，输入是本轮自有测试数据，
不是游戏配置事务，也没有触发 BackupManager、服务 pending 队列或 job。

随后在已有 Ubuntu ARM64 guest 中，经匹配的 Android linker64/Bionic 运行新C追加器，
两个独立比较进程均exit0。最终有效证据是 `linux-driver-v3.json` 捕获的真实
subprocess退出码，以及 `linux-prepare-v3.json` / `linux-reopen-v3.json`。
vmrun 的成功返回本身没有作为被测程序成功证据。

| 范围 | 真实结果 | 边界 |
|---|---|---|
| 8次追加 | 每次完整文件字节与Android记录一致，最终84bytes | 包名重复、空串、NUL、中文、代理对、未配对高/低代理项 |
| 跨进程重开 | 追加第9条后94bytes，与Android字节和SHA一致 | 普通进程重开；不是断电恢复 |
| modified UTF-8 | 2字节长度前缀，UTF-16 code unit编码 | 未将标准UTF-8代替Java writeUTF |
| overflow | Android抛UTFDataFormatException；Linux显式EOVERFLOW，原字节保留 | Android用65536个ASCII字符，Linux比较轮用21846个中文字符；均越65535编码字节上限，不宣称同一输入的异常全域对照 |
| 非法父路径/目录 | Android真实IOException/FileNotFoundException；LinuxENOENT/ENOTDIR/EISDIR | 只覆盖列明案例，未证明全部异常等价 |
| fsync | Android与Linux成功写入均有写前及写后fsync，返回0 | 无断电、磁盘故障或实际持久介质耐久性证据 |

84bytes SHA256：`5f56c6f93bdfae34f99757554e2017e19435c20cd072e0d67e839fa583d81242`。
94bytes SHA256：`7daa695e11315526d903c722e194be833581b205db6122a9b4aaa27ca1299725`。

## 真实系统调用与实现范围

此平台 `RandomAccessFile("rws")` 实测打开标志是 `O_RDWR|O_CREAT`，
不是依靠 `O_SYNC`。对目标journal的9次追加模式打开（8次成功+1次overflow），
以及重开阶段1次，按PID/文件描述符追踪到：open→fsync→seek末尾；成功写入再
write→fsync→close。临时文件创建和探针其他文件操作单独排除于这项计数，
不将所有包含journal路径的open混成追加次数。

新增 `backup_journal.c/.h` 仅实现带路径的UTF-16追加原语：长度检查、显式errno、
完整写循环、fsync和关闭。写入中途失败保留已写字节；主错误优先于close错误。
系统调用故障注入覆盖EINTR、短写、零写、部分写失败、fsync/seek/stat/open/close；
这些是工具测试，未冒充真实Android异常覆盖。

Android newJournal 的随机临时文件在本轮真实平台进程中创建，但Linux helper不实现
其命名/碰撞契约；也未实现service持锁、mPendingBackups/mJournal对象关联、
IOException吞掉并清journal、scheduler去重、提交或交付。
`preference_store_hook.c::unresolved_backup` 仍显式返回失败，未接入局部追加器。

## 环境、命令和可重验位置

宿主 Darwin arm64 / macOS15.6 / 38,654,705,664bytes内存。
Android `emulator-5580`，API30 / arm64-v8a，fingerprint为
`google/sdk_gphone_arm64/emulator_arm64:11/RSR1.240422.006/12134477:userdebug/dev-keys`。
平台services.jar SHA为
`c6992cdc2a3e23d5aeb6ab77e7a7f6889cfcf5c262c6ed86825e6fcbf601681d`。
真实框架进程PID8469/8492；原system_server PID522和游戏PID6901只读复核，未重启。
guest Linux aarch64，kernel `6.8.0-138-generic`。

私有实验根 `/private/tmp/nashura-journal-runtime-20261028-ea_t_4m4`；
Android工作目录 `/data/local/tmp/nashura-journal-ea_t_4m4`；
guest工作目录 `/tmp/nashura-journal-ea_t_4m4`。运行命令和日志SHA完整列于同阶段JSON。
复跑必须选择新目录/输出名，保留历史失败；以下变量仅用于缩写已执行路径：

```sh
SDK=/Users/gujiga/Library/Android/sdk
PRIVATE=/private/tmp/nashura-journal-runtime-20261028-ea_t_4m4
javac --release 8 -classpath "$SDK/platforms/android-35/android.jar" \
  -d "$PRIVATE/classes" tools/pure_linux/BackupJournalProbe.java
"$SDK/build-tools/36.0.0/d8" --min-api 30 \
  --lib "$SDK/platforms/android-35/android.jar" --output "$PRIVATE/dex" \
  "$PRIVATE/classes/BackupJournalProbe.class"
"$SDK/ndk/27.3.13750724/toolchains/llvm/prebuilt/darwin-x86_64/bin/aarch64-linux-android30-clang" \
  -std=c11 -Wall -Wextra -Werror tools/pure_linux/backup_journal.c \
  tools/pure_linux/backup_journal_probe.c -o "$PRIVATE/journal-probe"
```

编译均exit0；javac仅报告目标版本警告。AVD内实际命令形如：

```sh
CLASSPATH=/data/local/tmp/nashura-journal-ea_t_4m4/probe.dex:/system/framework/services.jar \
strace -f -qq -s 160 \
  -e trace=openat,write,writev,lseek,fsync,fdatasync,close,ftruncate \
  -o /data/local/tmp/nashura-journal-ea_t_4m4/prepare.strace \
  /system/bin/app_process64 /system/bin BackupJournalProbe prepare \
  /data/local/tmp/nashura-journal-ea_t_4m4/store
```

另进程以reopen模式运行。guest由 `guest_driver.py` 分别启动strace/Python比较器，
传入绝对路径probe和linker，真实参数数组与exit0见JSON。实际打开的Bionic库
SHA见JSON；本轮没有为helper重新采集完整 `/proc/maps`，不冒称完整映射审计。

## 保留的失败与验证

首轮Linux比较因传给Android linker的probe路径非绝对路径而失败；reopen随之因
缺少前阶段文件失败。修复为resolve绝对路径后v2 JSON通过，但退出码归档不充分；
最终v3以Python subprocess捕获两阶段退出码，均0、stdout/stderr为空。
此前阶段消息曾把首轮执行误认为通过，已核对日志纠正，以v3证据为准。
早期guest环境检查的FileNotFound另保留，不计为引擎失败。

本轮44项新增工具测试通过。独立源码副本全量专项689通过/1跳过，主工程211通过，
实际命令均exit0；公开九组1104通过/42跳过、exit0，集合不累加。
源码、DEX构建物、日志、比较器和runtime SHA绑定见机器证据。
私有APK、DEX、资源、原始trace和journal文件不发布；公开只保存源码和脱敏摘要。

## 严格门与下一步

本阶段未加载libg，未取得完整引擎/资源身份前后对照，未重放游戏配置事务。
**Linux Backup服务副作用绑定仍UNRESOLVED，CATALOG_READY_PASS=false。**
下一项最小工作为闭合服务queue/journal生命周期和scheduler实际契约，再把已验证
实现绑定到真实libg的新隔离子进程，自然初始化catalog。不得仅凭journal局部通过
将Backup回调改成成功，也不得手写ready或伪造JavaVM。

ResourceManager、GameState构造/setup/load、空场/reset/真实GameStateStep均NOT_RUN。
本轮没有no-VM、Android-free、完整对局、训练或10×证据。
