# E2.1-N：ART 系统服务观察器扩展与 guest 实验阻塞

日期：2026-10-30。接续原始 init 属性写入/读回与系统服务依赖阶段，扩展
`ArtSystemServicesProbe`，以便在可运行的原始 ART 服务进程中区分 Thermal、Power
服务发布、`SystemServiceRegistry` fetcher、Context 缓存及 `PowerManagerInternal`
的观察结果。

## 本轮源码与工程验证

- 探针新增 ThermalManagerService 启动与 Binder 查询、Power Binder/listServices
  检查、Power fetcher 类/方法与缓存槽观察、`PowerManagerInternal` 查询，以及阶段化
  的错误输出与 flush。
- fetcher 诊断会在隔离 probe 进程内检查缓存初始化状态；状态非初始值时会清除该
  Context 缓存槽，再调用 fetcher 作受控复查。复查输出必须与清理前状态一起解释，
  不代表未经干预的自然启动序列。
- 专项测试 `tools/pure_linux/test_probe_root_init.py`：6 passed。以 Java 17 API
  目标执行 `javac --release 17 -Xlint:all -Werror`：通过。JDK 25 默认目标的
  `-Werror` 会因 `System.loadLibrary` 的新 restricted API 提示而失败；该提示不在
  Java 17 目标范围内。
- Python 3.13.5 下纯 Linux 全集 `1138 passed、1 skipped`；公开检查所有组均通过
  （合计 `1104 passed、42 skipped`），主引擎 `211 passed`。公开源码清单校验
  通过（2,834 个文件），gitleaks 无泄漏，`git diff --check` 通过。

本轮没有在 ARM64 guest 执行扩展后的 ART 服务 probe，因此没有新的 Thermal/Power
服务运行结论，也没有新增 Binder、Context 或 fetcher 运行证据。源码与编译/单测通过
只表明工程材料可构建，不代表服务已启动。

## 原始 init policy 实验状态

已确认的 v22 只读计划与 `--apply` 实验均未开始。guest 的 sudo 认证被拒绝后，操作
停止；本轮没有新加载 SELinux policy、没有更改 sysctl、没有创建 v22 实验输出目录。
本记录不代表 guest 当前全局 policy 状态的重新检查；此前已有阶段记录须按其各自
时间与范围解释。用户可恢复有效的 guest 特权会话后，再从只读计划开始，并分别
保存 plan、apply 和状态恢复证据。

已完成的限定结果继续维持原边界：既有原始 init property 写入与同镜像 `getprop`
读回为 `PASS_LIMITED`；该结果不证明本轮服务 probe 成功。PowerManager、
DisplayManager、package 服务、GameApp Context、严格 catalog、ResourceManager、
GameState、Step、完整对局及性能仍未通过或未运行。

## 公开范围

公开仓库仅包含探针源码、工程验证记录和脱敏摘要；未加入登录凭据、guest 私有目录、
第三方二进制、policy、DEX、原始运行日志或 ART 归档。失败边界保留为
`BLOCKED_GUEST_PRIVILEGE`，不会将未运行实验记为通过。
