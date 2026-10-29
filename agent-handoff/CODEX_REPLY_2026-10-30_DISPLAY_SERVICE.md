# Codex 回执：Display 服务边界

- `DISPLAY_SERVICE_ENTRY_REACHED_LIMITED`：原始 init 与 APEX bootstrap 在 ARM64 guest 中稳定运行，三次 property 写入/同镜像读回、loop cleanup、sysctl 恢复和输入身份检查通过。
- 已进入 Display 依赖链并观察到 Power/Thermal/fetcher 前置结果；SurfaceFlinger 缺失导致原始 ServiceManager 等待，`system_services_ready=false`。
- `BLOCKED`：SurfaceFlinger/完整 system services、GameApp Context、严格 catalog、ResourceManager、GameState、Step、完整对局和性能。
- `NOT_RUN`：动作、快照、完整 match、Android 对照和性能验收。

详见 [报告](PURE_LINUX_DISPLAY_SERVICE_BOUNDARY_2026-10-30.md) 与 [证据](pure-linux-display-service-boundary-evidence-20261030.json)。
