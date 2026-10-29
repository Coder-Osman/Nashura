# E2.1-N：真实 SELinux/Binder 下的系统 Context

日期：2026-10-29。该阶段接续真实 ART/libg holder setup 实验，使用同一 ARM64
Android 30 SDK system image，未启动 Android 模拟器。运行时归档、原始 DEX、7 个
游戏库和 framework 输入均保留在本机私有目录，公开仓库只保存脱敏摘要。

## 结果

阶段状态为 `SYSTEM_CONTEXT_PASS_LIMITED`。

- guest 一次性启动参数启用 `selinux` LSM，`selinux=1 enforcing=0`；状态页显示
  `policy_load=0`、`deny_unknown=1`。
- binderfs 重建 `binder`、`hwbinder`、`vndbinder` 三个设备，协议版本为8。
- 同镜像原始 `servicemanager` 通过 `BINDER_SET_CONTEXT_MGR_EXT`，并读取同镜像
  `plat_service_contexts`。
- `ActivityThread.systemMain()` 返回；系统 Context 类型为
  `android.app.ContextImpl`，包名为 `android`，资源对象存在。
- `BinderInternal.getContextObject()` 返回 descriptor 为
  `android.os.IServiceManager` 的 Binder，`pingBinder()` 为 `true`。
- display、package 服务未启动，`GameApp.getInstance()` 仍为 `null`。

这些结果证明真实系统 Context 和 Binder context manager 已建立，但不等价于完整
Android 系统服务、应用 PackageManager、游戏 Context 或 ready 状态。

## 复现

公开 launcher 在 `tools/pure_linux/art_namespace.sh` 中增加可选的
`NASHURA_BIND_SELINUXFS=1` 分支：它只把宿主 guest 的 `/sys/fs/selinux` 绑定到
隔离闭包，并检查 SELinux magic `0xf97cff8c`。默认路径不改变。

系统 Context fixture 使用 JDK 与 D8 编译：

```sh
"$JAVAC" --release 8 -d "$BUILD" \
  tools/pure_linux/ArtFrameworkContextProbe.java
"$D8" --min-api 30 --output "$BUILD" \
  "$BUILD/ArtFrameworkContextProbe.class"
```

bootclasspath 在原有六个 core jar、conscrypt、framework-sdk、statsd 后追加同一
system image 提取的：

```text
/data/local/tmp/framework-tethering.jar
/data/local/tmp/framework-wifi.jar
```

启动顺序为：在独立 user/mount namespace 中挂载 SELinuxfs 和 binderfs，启动原始
`servicemanager`，等待其存活，再运行：

```text
ArtFrameworkContextProbe system
```

私有 final-v2 输出身份：

| 文件 | SHA-256 |
|---|---|
| stdout | `e1cd993efb29019c7043425bcd3f33b5a7e8c464af335c39a8be3041e6c50c5b` |
| stderr | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| exit | `9a271f2a916b0b6ee6cecb2426f0b3206ef074578be55d9bc94f6f3fe3ab86aa` |
| before/after input snapshot | `6888c0837f25ac9100fe0c4ac943bc28244e52afc021454910f839406b8c565d` |
| context fixture DEX | `24f3b2151048c88e182c5479c65b3b0e2ad415a67a9ba575e39842d1bdf9bfc8` |
| `servicemanager` | `a49a4e05a319889accd17f49c5b44bfb97eca4a647d8c76b9d24417532ff402d` |
| tethering framework jar | `754a9a6978f9d887010b84c99a68aced91825c1a96c853a71ae19913e84581f8` |
| wifi framework jar | `f6340cc5fd6948fa6103348263fc02d9cf602c04aa7ebf77ff3959d390f32772` |

机器摘要见[pure-linux-system-context-evidence-20261029.json](pure-linux-system-context-evidence-20261029.json)。

## 边界

`policy_load=0` 且 permissive 模式只证明 kernel SELinux 接口存在和可供原始
servicemanager 读取；不证明 Android policy 语义等价。display/package 服务仍需由
真实系统服务进程提供，当前实验没有伪造这些服务。provider 配置语义、catalog、
ResourceManager、GameState、Step、动作、快照、完整对局和性能仍未验收。
