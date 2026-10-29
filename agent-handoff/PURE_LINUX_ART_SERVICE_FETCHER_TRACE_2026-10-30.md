# E2.1-N：ART 系统服务观察器扩展与 guest 限定运行

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

随后在同一 ARM64 guest 的已加载 permissive policy 和隔离 namespace 中执行了扩展后的
`services` probe。原始 `android_servers`、System Context、Power/Thermal 服务、Binder
发布与列表、Power fetcher、Context 缓存槽和 `PowerManagerInternal` 均被观测到；probe
退出 0，所需观测集合完整。此结果是服务依赖的限定运行证据，不等价于完整
`system_services_ready`。

## 原始 init policy 实验状态

v22 的只读预检和 `--apply` 均已完成。预检确认 159 个输入身份匹配、policy 已加载且
保持 permissive；apply 中 `sys.system_server.start_count` 的 `1/2/3` 三次写入均由同
镜像 `getprop` 读回，输入身份保持不变，`kernel/kptr_restrict`、`vm/mmap_rnd_bits`
和 `vm/mmap_rnd_compat_bits` 均恢复原值。policy 仍保持已加载，按实验约定需 guest
重启后撤回。

apply 的服务结果为 `power_thermal_fetcher_probe_pass=true`，缺失观测为空；服务进程和
probe 均在清理阶段结束。原始运行归档和 strace 保存在私有临时目录，公开仓库只记录
脱敏摘要与哈希。

已完成的限定结果继续维持原边界：本轮服务 probe 成功只覆盖 Power/Thermal/fetcher
观察集合；`system_services_ready=false`，DisplayManagerService/SurfaceFlinger、
package 服务、GameApp Context、严格 catalog、ResourceManager、GameState、Step、
完整对局及性能仍未通过或未运行。

## 公开范围

公开仓库仅包含探针源码、工程验证记录和脱敏摘要；未加入登录凭据、guest 私有目录、
第三方二进制、policy、DEX、原始运行日志或 ART 归档。服务阶段状态为
`ART_SERVICE_FETCHER_TRACE_RUNTIME_OBSERVED_LIMITED`；完整服务和游戏验收门仍保持关闭。
