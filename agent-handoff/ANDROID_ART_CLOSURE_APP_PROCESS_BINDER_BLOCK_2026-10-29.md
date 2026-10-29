# Android ART 闭包启动与 Binder 阻塞

日期：2026-10-29。目标是验证 Android ART 闭包能否在同构 Ubuntu ARM64
guest 中启动 `app_process64`。本阶段只使用 `/dev/shm` 下的临时闭包，没有改动
冻结部署、仓库外工程或 VM 磁盘上的既有实验目录。

## 结果

闭包经过 `pivot_root` 后，`/proc` bind mount 成功，Android linker 的完整依赖
列表返回 0。补入与原 APK 同源的 `libstatssocket.so`、`libstatspull.so` 后，
`app_process64` 可以加载 `libandroid_runtime.so`、`libart.so` 及其依赖，发现 boot image
相关文件并选择 default boot image 配置（打印 `Using default boot image`），调用栈已进入 `JNI_CreateJavaVM`。随后 guest 没有
Binder 驱动：

```text
openat("/dev/binder", O_RDWR|O_CLOEXEC) = -1 ENOENT (No such file or directory)
Binder driver '/dev/binder' could not be opened.  Terminating.
Fatal signal 6 (SIGABRT) ...
```

该受控 probe 的 `app_process64 --version` 子进程退出码为 `134`（`SIGABRT`）。
使用普通文件作为 `/dev/binder` 的对照返回 `BINDER_VERSION` ioctl `ENOTTY`，
同样停在 Binder 初始化。`JNI_CreateJavaVM` 已被调用，但因 abort 没有返回可用
的 JavaVM 指针。

`Using default boot image` 只确认默认 image 配置选择；本轮未验证 boot image
完成装载。abort 日志同时记录 `Runtime does not yet exist!`，因此不将配置选择
记为 JavaVM 创建成功。guest 存在 `binder_linux.ko`，但模块未加载，Binder
设备节点和 binderfs 均未就绪。

## 输入身份

| 输入 | SHA-256 |
|---|---|
| 最小 system/APEX 闭包归档 | `f31376854ebf4955dab962d472d1e26e81288cba090d4ba3f7301a07d9f41f7b` |
| 含 boot image 的 ART 归档 | `f43aef19d5c5ab204c97797355402b3c7cedd05d05c0f5f1d580fcbdf96bd36a` |
| property addon v3 | `9b1df5adbbb5cd371cc4adb5041768e6587596f4d64ed013cd5125623f87eb24` |
| `libstatssocket.so` | `c70fa366743987548481e54c169c7d928c4d35d2fb1c37533498bd9f8fdd92ed` |
| `libstatspull.so` | `0ff86311f4be8c046548f07efe263f28c75c4d7589af3d49d36dafd4a833252a` |

外层 `strace -f` 的最终独立复跑归档 SHA-256 为
`036bd5ec82d8f1204c53513c82c76d6dd625638675cd00cf3573c7b95909239a`；占位普通
文件对照 trace SHA-256 为
`2c06002fb0c1d59fd86ff6211cba690adb52abc07628c4e754fb4a1bc0a538c4`。原始
strace 保存在本机临时证据目录，不发布到公开仓库。

## 门状态

| 项目 | 状态 |
|---|---|
| ARM64 linker 与完整 NEEDED 依赖解析 | `PASS_LIMITED` |
| `/proc` bind mount 与 `pivot_root` | `PASS_LIMITED` |
| `app_process64` 装载 ART 核心库并选择 default boot image 配置 | `PASS_LIMITED` |
| `JNI_CreateJavaVM` 入口 | `REACHED_ABORT_BOUNDARY` |
| `app_process64 --version` 完整启动 | `BLOCKED_GUEST_BINDER` |
| Android JavaVM 返回及 JNI 生命周期 | `NOT_RUN` |
| Linux holder setup | `RUNTIME_BLOCKED_JAVAVM`（沿用上一阶段） |
| `CATALOG_READY_PASS` | `false` |
| ResourceManager、GameState、Step、完整对局、性能 | `NOT_RUN` |

占位普通文件只能证明缺少 Binder 设备接口，不能替代 Binder 驱动或伪造
JavaVM。下一步需要在具备真实 Binder 驱动的 Android 进程中建立 loader 生命周期，
再重新验证 holder setup；本阶段不宣称 `app_process64` 或 Linux JavaVM 已通过。

## 发布验证与清理

基线提交 `9e37d1b`。`git fetch origin --prune` 后，工作分支远端仍指向此提交，
`origin/main` 为其祖先 `1417278`。本轮公开变更在独立 Nashura 仓库维护。

维护检查均退出 0：

| 命令或公开回归分组 | 结果 |
|---|---|
| `python tools/check_public_snapshot.py` | 2792 文件通过 |
| `python tools/run_public_checks.py`：public | 794 通过，27 跳过 |
| affordability | 23 通过 |
| selection timeout | 82 通过 |
| position guards | 15 通过，3 跳过 |
| live command | 25 通过，4 跳过 |
| dispatch command | 31 通过，3 跳过 |
| reservation mapping | 31 通过，2 跳过 |
| command coordinates | 38 通过，3 跳过 |
| collection coordinator | 65 通过 |
| `python tools/run_main_engine_checks.py` | 211 通过，38.77 秒 |
| `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tools/pure_linux` | 1105 通过，1 跳过，17.64 秒 |
| `gitleaks dir . --config .gitleaks.toml --redact` | 无泄漏 |
| `git diff --check` | 通过 |

9 份公开 XML 的 testcase、结果和跳过原因保持一致，只更新本轮运行元数据。
这些 CPU 工程回归与原生 ART/Binder 阻塞证据分别记录，均不关闭整体运行门。

按用户确认清理本轮 host/guest ART 临时归档及展开目录，保留 host 的原始 trace、
探针脚本和诊断副本于 `/private/tmp/nashura-art-followup-20261029`。删除前核对
host/guest 三份主要归档 SHA 一致，修正最小闭包哈希的抄录错误。清理后 guest
`/dev/shm` 使用量降至 99 MiB；冻结部署和既有实验目录保持原样。
