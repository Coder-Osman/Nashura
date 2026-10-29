# E2.1-N：真实系统服务启动边界

日期：2026-10-29。本轮在同一 ARM64 Ubuntu guest、同一 Android 30 system image、
真实 SELinuxfs、binderfs 和原始 `servicemanager` 下，尝试进入同镜像
`SystemServer`/`DisplayManagerService`。没有注入假的 PackageManager、DisplayManager、
property service 或 ready 标志。

## 结果

阶段状态为 `SYSTEM_SERVICES_BLOCKED_LIMITED`。

- 原始 `services.jar` 已从 `/system/framework/services.jar` 只读提取，大小
  `14,712,067` 字节，SHA-256 为
  `c6992cdc2a3e23d5aeb6ab77e7a7f6889cfcf5c262c6ed86825e6fcbf601681d`。
- `SystemServer.main()` 真实进入 `SystemServer.run()`，在设置
  `sys.system_server.start_count` 时因隔离闭包没有 Android property service 退出；
  这是 `SystemProperties.native_set` 的真实 `RuntimeException`，不是探针主动拒绝。
- `System.loadLibrary("android_servers")` 的第一轮真实失败为缺少
  `libadb_pairing_server.so`。从同镜像 `com.google.android.adbd.apex` 只读提取
  pairing 库并补齐依赖后，加载继续解析到 ART/system namespace 的更多原生库；
  当前不完整闭包最终在 linker 阶段以 `SIGSEGV` 退出，未启动 DisplayManagerService。
- 因此 display/package 服务、真实应用 PackageManager、`GameApp` Context、catalog、
  ResourceManager、GameState、Step、动作、快照和完整对局仍未验收。

## 探针

`tools/pure_linux/ArtSystemServicesProbe.java` 只调用原始框架入口并报告阶段：
`SystemServer.main()` 或 `System.loadLibrary("android_servers")` 后构造真实系统 Context、
`SystemServiceManager` 和 `DisplayManagerService`。它不伪造服务，也不把失败转换为成功。

## 证据

私有运行输出保存在 `/private/tmp/nashura-art-rebuild-20261029/system-services-v1/`，
含 property-service 失败、missing ADB pairing 失败和补依赖后的 linker `SIGSEGV`。
输入快照在每次运行前后字节一致。pairing 库均来自原始同镜像 APEX，未修改文件内容。

## 边界

`servicemanager` 和系统 Context 的上一阶段 `SYSTEM_CONTEXT_PASS_LIMITED` 仍有效；
本轮没有把 property service 或 `libandroid_servers.so` 的加载失败解释成完整 Android
服务启动。下一步需要完整的原始 init property service、APEX namespace 和系统服务依赖
闭包，之后才能继续验证 package/display 以及游戏 Context。
