# E2.1-N：Display statsd linker 依赖复验

日期：2026-10-30。分支：`codex/pure-linux-libg-headless-20261026`。
本轮针对上一轮 Display probe 的两个 statsd APEX 库不可见问题，使用新的 r5
输入目录完成一次真实 ARM64 Ubuntu guest 复验。

## 输入与隔离边界

- `libstatssocket.so` SHA-256：
  `c70fa366743987548481e54c169c7d928c4d35d2fb1c37533498bd9f8fdd92ed`。
- `libstatspull.so` SHA-256：
  `0ff86311f4be8c046548f07efe263f28c75c4d7589af3d49d36dafd4a833252a`。
- 两个文件均与同一闭包中的 `system/apex/com.android.os.statsd/lib64` 原件逐字节
  一致，并以只读 bind 方式呈现为 `/system/lib64` 库。
- SurfaceFlinger、GPUService、allocator、composer 和 ART probe 均在独立
  mount/PID/cgroup/network/IPC namespace 中运行；实验上限为20秒。
- 使用已加载的 permissive policy；本轮没有写入或重新加载 policy。实验前
  `enforce=0`、`policy_load=1`，结束后仍为相同状态。

## 结果

property 连续请求 `1、2、3` 均收到 ACK，并由同镜像 `getprop` 独立读回；结果状态为
`INIT_PROPERTY_CONTINUITY_PASS_LIMITED`。693个固定输入身份未改变，三个 sysctl
均恢复原值，`system_services_ready=false`。

SurfaceFlinger 和 GPUService 的 strace 均记录成功打开：

```text
/system/lib64/libstatssocket.so
/system/lib64/libstatspull.so
```

这证明本轮的 statsd linker 可见性修复已在真实 guest 中生效，状态为
`DISPLAY_STATS_LINKER_VISIBILITY_PASS_LIMITED`。SurfaceFlinger 后续仍因 guest
没有 `/dev/goldfish_pipe`、OpenGL host pipe 和可用 EGL host 连接而触发
`eglInitialize`/`eglQueryStringImplementationANDROID` 失败并以127结束；composer
记录 framebuffer 设备不存在并以1结束。allocator 以234结束，GPUService 在清理前
保持运行。probe 仍等待不到 SurfaceFlinger Binder，因此不构成 Display 服务通过。

## 证据与清理

完整运行归档只保存在主机私有临时目录：

`/private/tmp/nashura-display-root-trial-20261031-r5.tar`

归档大小为111,462,400字节，SHA-256为
`0f9020ed9a8d48b0985c421e1c57e6e0677af0f7b277301ce45ee6e5bd631674`。
归档复制并校验后，guest 中本轮输出目录、settings、stdout/stderr 和临时归档均已
清理；既有 guest policy 保持原样。guest 根盘本轮接近满载，后续实验前需要先释放
空间或使用更大的独立目录。

## 工程验证与未闭合门

本轮源码专项 `tools/pure_linux/test_probe_root_init.py` 为 `8 passed`。使用
`/opt/anaconda3/bin/python3` 的发布环境复验：纯 Linux 全集 `1140 passed、1 skipped`；
公开主组 `794 passed、27 skipped`，affordability `23 passed`、selection-timeout
`82 passed`、position-guards `15 passed、3 skipped`、live `25 passed、4 skipped`、
dispatch `31 passed、3 skipped`、reservation `31 passed、2 skipped`、coordinates
`38 passed、3 skipped`、collection-coordinator `65 passed`；主引擎 `211 passed`，
gitleaks 无泄漏，公开快照校验通过。严格
`CATALOG_READY_PASS`、GameApp Context、ResourceManager、GameState、Step、完整
对局、Android 对照和性能仍未通过或未运行。

## 公开范围

公开仓库只包含 launcher、身份检查、测试、脱敏报告和机器摘要；不包含 guest 凭据、
policy、DEX、第三方 ELF、原始 strace 或 guest 私有目录。
