# E2.1-N：Android 与 Linux 实际 catalog 行数差异

基线 `0b33fdf271cfb96613d67c68524722d2955e3380`。本阶段取得实际差异，
**不是 catalog 通过**。两个进程均使用同一 SHA 的真实 ARM64 libg，分别在
Android API30 与现有 Ubuntu ARM64/Bionic 运行，采集时 context ready 字节均为1。

## 实际结果

| 指标 | Android 隔离实例 | 既有 Linux loader |
|---|---:|---:|
| 非空表槽 | 162 | 162 |
| 对象行数合计 | 25,810 | 24,353 |
| 不同表槽 | 17 | 17 |
| 已记录 CSV 行列维度不同 | 0 | 0 |

行数差额1,457；槽212和214在 Android 分别222和231行，在 Linux 均为0。
Android 与 Linux 的162个槽集合相同，其他差异槽为9、10、22、34、35、123、
133、166、173、179、187、189、191、205、213。
这些只读数值来自受边界、owner、type和非空指针检查的表结构；它们不是单元格值。
容量可能因分配策略不同，本次不将容量差异作为语义差异。

Android 截获两次布局完全一致；`status` 报告 coldReady/contentReady/published 且
未配置对局，读取同一已发布 context。Linux 为此前独立进程自然 loader 结果。
Android 此次启动的 update 集合未重新核验，且两个运行环境、初始化流程及采样
时间并不完全相同，因此**尚不能归因于 Linux 实现缺失**。历史资源身份相同也
不代表此次启动后实际解析结果相同；必须逐项验证本次输入与调用路径。

## 实验与失败记录

专用 `e1-only / emulator-5630` 以只读、无快照保存模式启动，核验已安装 APK 与
libg SHA；只使用 status 命令和 Frida 只读内存，没有调用对局动作或配置 store。
本轮结束时进程与模拟器已退出。原始地址、APK和日志保存在本机私有目录。

首次 v1 客户端16.7.19与已有server17.18.0不兼容，spawn前失败 exit1。
v2 改用已有16.7.19 server，spawn超时 exit1，但出现本轮唯一新 engine0 进程；
v3 核验 PID和 status 后只读附加，该进程 coldReady、contentPublished 为真，
采集成功 exit0。16.7.19 server SHA `4eebf1fbc66ff54aba9a9124c2ef8b32b566616388c60e2caa65148a529d826a`。
安装匹配17.18.0客户端的下载尝试被中断；两个失败结果及退出码保留。

Android result SHA `4a9dbb681c1c3c5d02afbe7d530e9039df0f3e4382fa287e3318962bd9cf7c4c`；
Linux stdout SHA `2adeafc44e3c03b5a62954d863ede2ea2971656aa01f519bc614ab863919a6ff`；
比较结果 SHA `66f7e26662c2f955dbd08903492de8e856768eb397348084a97514cddaeb7956`。

```sh
python tools/pure_linux/run_android_catalog_layout.py --adb "$ADB" --engine "$MATCHED_LIBG" \
  --script tools/pure_linux/android_catalog_layout.js --output "$PRIVATE_V3" \
  --serial emulator-5630 --frida-address 127.0.0.1:27066 --control-port 28996 --owned-pid 4454
# exit 0
python tools/pure_linux/compare_catalog_layout.py --android "$PRIVATE_V3/result.json" \
  --android-sha256 4a9dbb681c1c3c5d02afbe7d530e9039df0f3e4382fa287e3318962bd9cf7c4c \
  --linux "$LINUX_STDOUT" --linux-sha256 2adeafc44e3c03b5a62954d863ede2ea2971656aa01f519bc614ab863919a6ff \
  --output "$PRIVATE_V3/comparison.json"
# exit 2: differences detected
```

专项 `python -m pytest -q -p no:cacheprovider tools/pure_linux` exit0，1000通过/1跳过。
下一步对齐 Android 本次 update 身份与 Linux 运行阶段，追踪17个槽的行创建及后处理，
先定位差异再尝试修复。`CATALOG_READY_PASS=false`，ResourceManager、GameState、
Step与完整对局仍未进入。
