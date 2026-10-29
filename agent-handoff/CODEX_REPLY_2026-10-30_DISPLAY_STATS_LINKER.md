# Codex 回执：Display statsd linker 复验（2026-10-30）

- `DISPLAY_STATS_LINKER_VISIBILITY_PASS_LIMITED`：SurfaceFlinger 和 GPUService 均从
  `/system/lib64` 打开同源 `libstatssocket.so`、`libstatspull.so`。
- `INIT_PROPERTY_CONTINUITY_PASS_LIMITED`：property `1/2/3` 同镜像读回通过，693 个
  输入身份不变，三个 sysctl 恢复。
- `BLOCKED`：SurfaceFlinger 受 EGL/goldfish pipe 缺口限制，composer 受 framebuffer
  缺口限制，系统服务 readiness 与 Display Binder 仍未闭合。
- `NOT_RUN`：严格 catalog、ResourceManager、GameState、Step、完整对局、Android
  对照和性能验收。

完整归档只保存在主机私有临时目录；公开仓库仅保存脱敏报告和机器摘要。
