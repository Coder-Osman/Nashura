# E2.1-N：原生 Backup 队列发布中断恢复

基线 `3c9dece851851e7ba3ed4de28d6b740e1c2be884`，工作分支
`codex/pure-linux-libg-headless-20261026`。本阶段为 **PASS_LIMITED**，只验证
原生候选队列在一个确定发布边界被进程中断后的恢复，不代表 Android Backup 等价。

## 问题与实现

队列通过 `linkat` 将 staging 发布为 pending，再删除 staging。进程在两步之间
退出时，两个名字指向同一 inode、链接数为2；现有生产者和消费者均拒收并保留现场。
新增显式 `native_backup_recover.py`：按 preference→backup 顺序取得非阻塞锁，
核对目录、两文件身份和权限、请求格式、相同 inode 与恰好两个链接，复查路径后
只移除 staging 并同步目录，保留 pending。不同 inode、损坏、额外链接、权限异常、
符号链接和活跃锁保持拒收；它不自动修复其他中断状态。

锁约束适用于遵守该协议的进程，不声称抵御有同一用户权限的非协作进程。恢复后
I/O 失败仍可能留下需要再次检查的现场；不把 fsync 成功等同于断电实验。

## ARM64 Linux 实验

Ubuntu guest `aarch64`、Linux `6.8.0-138-generic`，现有 API30 linker/Bionic。
独立 probe 编译真实 `native_backup_queue.c`，只在成功 `linkat` 后以 `_exit(71)`
受控中断；输入为两份隔离合成 XML，没有加载游戏或调用 Android 服务。

- probe 退出71，pending 与 staging 是同一 inode，链接数2。
- 既有消费者明确拒收未完成入队；显式恢复返回 `PENDING_RECOVERED`。
- pending SHA 恢复前后相同，链接数变为1；再次请求退出0且 `created=0`。
- 独立消费者校验并归档两份测试 XML，确认并移除 pending；仅限原生文件系统归档。
- runner exit0；结果 SHA256 `6d0507e912050c783fee09d9e94559336c7607349154349fe35aba68b845cba0`。
- probe SHA256 `4271f17539ab456cbfccb894f036c6a084a25f230737287897b668967ecc5155`；
  linker SHA256 `e6a2026a6252e0a80c242555e147faca98cd557e2adac1927cd5c096d70ce4c7`；
  libc SHA256 `9fe640090067ed1cb993815eb5d99fc03d80076754deafa813d0fd78dbb40569`。

首次尝试 guest `/usr/bin/cc`，VMware Tools exit255、文件不存在；没有产生 probe。
随后使用本机已有 NDK 27.3 的 `aarch64-linux-android30-clang`，编译 exit0：

```sh
"$NDK_CLANG" -Wall -Wextra -Werror -O0 -g -fPIE -pie -pthread \
  -Wl,--wrap=linkat tools/pure_linux/native_backup_crash_probe.c \
  tools/pure_linux/native_backup_queue.c -o "$PRIVATE_PROBE"
python3 run_native_backup_crash.py --probe "$GUEST_PROBE" --linker "$LINKER" \
  --libs "$LIBS" --output "$EXCLUSIVE_RUN"
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
```

最后一项 exit0，**988 passed、1 skipped**；定向测试25通过。guest 私有实验根
`/tmp/nashura-native-backup-crash-20261028-v1`；输出文件及 synthetic spool 保留。
公开不包含运行时二进制、原始游戏库或私有配置。

## 未解决

这次没有对真实游戏 store 重复写入，也没有在自然 libg loader 中注入中断。
Android Backup 服务的调度、交付和异常域与此候选后端仍未证明等价。
`configuration_contract_pass=false`、`catalog_ready_pass=false`；ResourceManager、
GameState、Step、完整对局及性能均未验收。

[机器证据](pure-linux-backup-crash-recovery-evidence-20261028.json)。
