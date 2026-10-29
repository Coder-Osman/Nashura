# E2.1-O：原始 DisplayManagerService 入口与 SurfaceFlinger 边界

日期：2026-10-30。基线为 `285f441`，目标分支为
`codex/pure-linux-libg-headless-20261026`。本轮在同一 ARM64 Ubuntu guest、已加载
且 permissive 的同镜像 SELinux policy 下，复用 service-v2 工具链，在独立
mount/PID/cgroup/network/IPC namespace 中运行原始 init 和 Display probe。

状态：`DISPLAY_SERVICE_ENTRY_REACHED_LIMITED`；`system_services_ready=false`。

## 真实 guest 结果

- root-only preflight 通过：160 个输入身份匹配，policy SHA 为
  `9f9115c47be1a90556dabaf6339127c5b626bd2fa2a26687ee0733bbb790d0bc`，
  `enforce=0`，起始 sysctl 为 `kptr_restrict=1`、`mmap_rnd_bits=33`、
  `mmap_rnd_compat_bits=16`。
- 原始 init 的 `sys.system_server.start_count=1、2、3` 三次请求均收到 ACK，
  并由同镜像 `getprop` 独立读回；输入身份保持不变，三个 sysctl 恢复，guest
  policy 仍保持已加载/permissive。`kernel_after.kptr_restrict=1` 与原值一致。
- 原始 `apexd-bootstrap` 成功，strace 记录 5 个 bootstrap APEX 实际挂载，
  exit 0；本轮没有 v24 在 `/dev/shm` 的 loop 节点 `EACCES`。新建 loop 全部移除，
  预存 loop 状态不变，cleanup_errors 为空。
- Display probe 已进入原始服务依赖链：ART、System Context、Power/Thermal
  Binder、Power 服务列表、Power fetcher/factory、Context 缓存槽和
  `PowerManagerInternal` 均有输出。随后原始 ServiceManager 在 `/dev/binder`
  等待不存在的 `SurfaceFlinger`；probe strace 记录 104 次等待，反复出现
  `ENOTCONN`，并记录“Service SurfaceFlinger didn't start”。未出现
  `ART_DISPLAY_SERVICE_STARTED=true` 或 Display Binder 标记，9 秒 probe 上限后由
  runner 收尾。该结果证明了入口前置依赖和首个缺口，不证明 DisplayManagerService
  或完整 system services ready。
- 服务摘要为 `system_services_ready=false`；GameApp Context、严格 catalog、
  ResourceManager、GameState、Step、动作/快照、完整对局、Android 对照和性能
  仍为 `NOT_RUN` 或未通过。

## 证据身份

公开仓库只保存脱敏摘要，不发布 policy、DEX、第三方 ELF、原始运行日志、设备节点
或 guest 凭据。私有证据包及 strace 保留在本机临时目录；精简归档 SHA-256 为
`ee458f3ed407cc1df2a1d1eabdca984a38e20a0cedad0e2f29b68d2691f57d39`。

机器摘要见[pure-linux-display-service-boundary-evidence-20261030.json](pure-linux-display-service-boundary-evidence-20261030.json)。

## 工程验证

- `/opt/anaconda3/bin/python3 tools/run_public_checks.py`：公开主组
  `794 passed、27 skipped`，其余公开分组均 exit 0；affordability 分组
  `23 passed`。
- `/opt/anaconda3/bin/python3 tools/run_main_engine_checks.py`：`211 passed`。
- `/opt/anaconda3/bin/python3 tools/check_public_snapshot.py`：`2837` 个公开源文件
  校验通过。
- `gitleaks dir . --config .gitleaks.toml --redact`：未发现泄漏；`git diff --check`：通过。

这些工程测试不替代真实 Display/SurfaceFlinger 依赖，也不改变整体验收门。
