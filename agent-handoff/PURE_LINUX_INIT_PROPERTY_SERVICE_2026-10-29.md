# E2.1-N：原始 init property service 请求边界

日期：2026-10-29。接续系统服务启动边界，在 ARM64 Ubuntu Linux 上调用同一
Android 30 SDK 镜像的原始 `/system/bin/init second_stage`，未启动 Android 模拟器。

## 结果

阶段状态为 `INIT_PROPERTY_REQUEST_REJECTED_LIMITED`。三次独立输出目录的
v2/v3/v4 实验均由原始 init 生成 property area、`property_info` 和真实 Unix
property socket；探针发送 Android v2 协议请求
`sys.system_server.start_count=1`，服务端实际接收后返回 `24 / 0x18`
（`PROP_ERROR_PERMISSION_DENIED`）。strace 记录了 accept、完整请求接收和响应发送。

这推进了上一阶段“没有 property service”的边界，但尚未获得可持续、可写的
property service。init 最终均以 `6` 退出，未发生超时。property write readback、
SystemServer、display/package、GameApp Context、严格 catalog、ResourceManager、
GameState、Step、动作、快照、完整对局、Android 基线对照和性能仍未验收。

## 真实输入与环境

- init SHA-256：`1f0964ea07e28a6c3b236b1b29c80a42972b392d9d3b3b48398904563c35bd67`。
- vendor 输入从同一 SDK 的原始 vendor.img 只读提取；镜像 SHA-256：
  `1953bc61224565d08deac8624502b676b30712fd8bd6c7ee95e415a1d8015a09`。
  原始 vendor SELinux platform version 为 `30.0`。关键 version/context 文件经
  debugfs 独立读取核对，未以自建内容补齐。
- 每次独立 properties/socket/log 输出，保留早期 ART property 文件；
  `property_info` 为 44,704 字节，三次 SHA 一致。
- SELinuxfs 是真实挂载，观测到 `enforce=0`、`policy_load=0`、`deny_unknown=1`，
  没有 `property_service` class；原始日志明确报告 `Unknown class property_service`
  和 `SELinux permission check failed`。
- init 后续尝试写真实 `/proc/sys/kernel/kptr_restrict` 时权限不足，日志
  `Unable to set adequate kptr_restrict value!` 后 fatal。观测前后该值均为 `1`。
  同时仍有 PID1/cgroup、subcontext 和系统属性文件闭包缺口。

本轮没有加载 SELinux policy、修改 sysctl、替换原始 init、伪造系统服务或注入
成功属性。kmsg 输出是普通文件日志 sink，仅捕获 init KernelLogger 输出，
不把它描述为内核设备。探针检查的 22 个输入文件 SHA 和所列 SELinux/sysctl
状态前后相同；该检查覆盖指定输入与状态，不代表整台 guest 的全量快照。

## 复现与证据

使用 `tools/pure_linux/probe_init_property_service.py`，提供原始 ART 闭包、同镜像
vendor、`art_namespace.sh`、新的绝对输出目录及上述 init SHA。探针校验输入身份，
拒绝复用输出目录和向输入目录写输出，并保留 strace、日志、命令、退出码及结果。
退出 0 只表示观测完成且指定输入/状态未变；即使收到写入 ACK，也保持 readback
和后续 Android/游戏门关闭。

公开脱敏摘要：`pure-linux-init-property-service-evidence-20261029.json`。
原始私有日志保存在本机 `/private/tmp/nashura-init-property-20261029/v2`、`v3`、
`v4`，公开仓库只记录其 SHA 和必要观测，不包含第三方镜像、policy 或库。

原始源码依据（Android 11 `android11-release`）：

- [main.cpp](https://android.googlesource.com/platform/system/core/+/refs/heads/android11-release/init/main.cpp)：`second_stage` 入口。
- [property_service.cpp](https://android.googlesource.com/platform/system/core/+/refs/heads/android11-release/init/property_service.cpp)：v2 请求与真实 SELinux `property_service/set` 检查。
- [security.cpp](https://android.googlesource.com/platform/system/core/+/refs/heads/android11-release/init/security.cpp)：`SetKptrRestrictAction` 的 fatal 分支。

后续须继续补齐真实服务依赖，或在独立内核环境满足原始 init 的 policy/PID1
条件，再验证 property readback、package/display 和游戏 Context；不得从 socket
存在或工程测试通过推断游戏初始化成功。

## 工程验证

Python 3.13.5 下纯 Linux 专项为 `1128 passed、1 skipped`（新增 9 项协议/路径/
验收门测试），公开主组 `794 passed、27 skipped`，主引擎 `211 passed`。
公开 affordability、selection timeout、position guards、live、dispatch、reservation、
coordinates、collection coordinator 分组均通过；源码快照、shell 语法、gitleaks
和 Git whitespace 检查通过。以上为工程检查，不代替真实 Android 服务或完整游戏验收。
