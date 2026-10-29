# E2.1-N：原始 init 的真实属性写入与读回

日期：2026-10-29，基线 `3ea37a6`。原始 Android 30 init 已在 Ubuntu ARM64
guest 内通过真实 property v2 协议写入，并由同镜像 toolbox/getprop 独立读回。
连续写入 `sys.system_server.start_count=1、2、3` 的限定验证通过；服务启动期间的
property 持续可用仍受原始 APEX 启动闭包阻塞。系统服务、GameApp Context、严格
catalog、ResourceManager、GameState、Step、完整对局及性能验收均保持未通过。

## 已验证结果

- v1、v2、v3 均有真实写入 ACK 与独立 getprop 读回。v4 在同一 init 生命周期内
  依次写入并读回 `1、2、3`，init 最终由 20 秒上限终止。v4 服务探针因为 CPU ABI
  属性缺失在进入 Java 前退出 0；空输出和 exit 0 不记为服务成功。
- 补齐同镜像 `system/build.prop` 与 `system/etc/prop.default` 后，公开脚本的
  property 专项可连续写入并读回三个值；该专项不证明 20 秒服务可用或完整启动。
- 真实 `/sys/fs/selinux/create` 已计算 `init → linkerconfig / apexd` 域转换，
  原始 linkerconfig 与 `apexd --bootstrap` 均退出 0。APEX bootstrap 在当前闭包
  中激活了零个包，此 exit 0 不表示 ART APEX 就绪。
- 全部 root init 实验均在独立 mount/PID/cgroup/net/IPC namespace 中进行；
  init 的最长运行时间为 20 秒。`kptr_restrict=1`、`mmap_rnd_bits=33`、
  `mmap_rnd_compat_bits=16` 均已恢复。policy 保持已加载、enforce=0；未自动重启 guest。
- 首次实验按用户确认加载了同镜像原始 policy，之后复用其固定身份。
  公开 `probe_root_init.py` 只接受已经加载且 SHA 匹配的 policy，默认只输出计划。

## 修复依据与实现

原始 libselinux 的 `verify_selinuxmnt` 要求 selinuxfs mount 为 rw，
`security_compute_create_raw` 则需以 `O_RDWR` 打开真实 `/create` 计算接口。
因此保留该内核计算接口，同时逐项将 policy/enforce/boolean/AVC 配置节点 bind 为
只读，其他 sysctl 与 sysrq-trigger 也保持只读。仅三个原始 init 必需 sysctl
临时可写，并在 `finally` 中恢复和复核。

`namespace_exec.c` 在 pivot_root 后卸载 oldroot，使用真实 ELF 的 PT_INTERP
执行程序；原先直接调用 linker 的方式让 `/proc/self/exe` 指向 linker，影响 init
的 vendor subcontext。新增可选数字 UID/GID 降权只用于服务子进程。
其余改动包括独立 devpts、原始 ELF 标签恢复，以及 observer 异常和超时收尾记录。
property ACK、匹配读回、系统服务就绪分别判断。

技术依据为 [Android 11 libselinux init.c](https://android.googlesource.com/platform/external/selinux/+/refs/heads/android11-release/libselinux/src/init.c)、
[compute_create.c](https://android.googlesource.com/platform/external/selinux/+/refs/heads/android11-release/libselinux/src/compute_create.c)、
[init mount namespace](https://android.googlesource.com/platform/system/core/+/refs/heads/android11-release/init/mount_namespace.cpp)
与同镜像 ELF/Dex/strace。本会话工具集中没有 Context7，源码依据直接读取 AOSP。

## 服务失败与下一处边界

1. 补齐 CPU ABI 属性后，原始 Java 探针已加载 android_servers。以 guest root
   注册 Binder context manager 返回 EPERM；将服务进程降回 binderfs 设备既有
   guest UID 后，真实 servicemanager 与系统 Context 恢复。
2. PowerManagerService 仍在 BatterySaverController 的缓存属性写入处失败，
   本轮原因是 socket 连接拒绝。init 已因 BoringSSL 自检的解释器缺失进入退出路径。
3. 原始 init 会把 `/apex` 覆盖成新 tmpfs。当前 SDK 提取闭包没有 `/system/apex`
   中供原始 apexd 激活的 APEX 包；`boringssl_self_test64` 虽已补入，但其解释器
   `/system/bin/linker64 → /apex/com.android.runtime/bin/linker64` 在 init 自身的
   mount namespace 中仍不存在。`update_linker_config` 的 devpts 问题已修复。
4. 另一次实验加入五个原始 manifest 和 112 个提取文件组成 flattened 目录。
   原始 vendor/build.prop 明确为 `ro.apex.updatable=true`，原始 init 因此跳过
   flattened 激活，失败保持。该属性未改写，也没有注入自检成功或 coldboot ready。
5. 下一步补齐原始可更新 APEX 包、真实激活所需设备及挂载依赖，先验证 BoringSSL
   自检返回，再处理 ueventd/coldboot 和 cgroup 条件；随后复跑 PowerManager / DisplayManager。

失败归档和最初 1,053 个普通文件、44 个符号链接的回退归档均保留。公开仓库只含
源码和脱敏 SHA 摘要，不包含第三方 ELF、原始 policy、DEX、资源或原始运行日志。
APEX、属性输入和 init 运行输出均位于实验克隆；guest 的全局 policy 变化及
sysctl 恢复结果按上文记录，运行中的冻结部署保留。

## 复验入口

```sh
"$NDK_CLANG" -static -Os -Wall -Wextra -Werror \
  tools/pure_linux/namespace_exec.c -o "$TOOLS/nashura_namespace_exec"
python3 tools/pure_linux/probe_root_init.py --settings "$PRIVATE_SETTINGS" --output "$NEW_OUTPUT"
# 核对计划后，在已有匹配 policy 的 guest root 中执行同命令并加 --apply。
```

`PRIVATE_SETTINGS` 为本机 JSON，包含 canonical absolute paths：`closure`、`vendor`、
`launcher`、`tools`、`backup`，以及 `loaded_policy_sha256` 与 `identities` 字典。
字典键为文件绝对路径，值为 SHA-256；必须覆盖 init、原始 policy 输入、两个原始
属性文件、launcher、toolbox、namespace helper、回退归档。建议同时固定所有
原生库、APEX manifest、framework、探针和原始 rc；`inputs_unchanged` 仅覆盖
字典列出的文件及 property probe 自身记录的输入。

服务观察另需 `services=true`、`linker_config`、非特权 `service_uid/service_gid`，
以及已编译为 `TOOLS/art-system-services.dex` 的现有 Java 探针。相应服务程序和
配置文件也须列入身份字典。脚本不以服务进程存活或 exit 0 设置 ready。

Python 3.13.5 工程复验：纯 Linux `1133 passed、1 skipped`；公开主组
`794 passed、27 skipped`，其余公开分组全部 exit 0；主引擎 `211 passed`。
这些是工程门，实际 property、服务失败和整体游戏状态见
[机器摘要](pure-linux-init-property-readback-evidence-20261029.json)。
