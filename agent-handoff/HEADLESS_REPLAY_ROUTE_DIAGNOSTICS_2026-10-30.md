# Headless replay 路径诊断

日期：2026-10-30

状态：`HEADLESS_REPLAY_ROUTE_DIAGNOSTICS_OBSERVED_LIMITED`

整体目标完成：否

## 结果

在同一固定 ARM64 guest fixture 中，分别使用 replay 原有 `battle.cmd` 和 runner 将
相同 52 条命令转为显式注入的 `cmd`，两条路径均到 tick 3601、原生 finalized=1，
正常退出。终态摘要均为 `6c959326dd2f09cc`、3091 字节；这是终态摘要相同，
不等同于全局状态或所有调用路径完全等价。

两条路径都在 step 开始时有 52 条队列命令，结束时有 2 条。50 条命令在 31 个
队列变化步中被消费，首次变化在 tick 21，末次在 tick 3521。tick 200 时对象数为 9，
双方手牌观测发生变化；tick 3601 时对象数为 12，原生 finalized 标志为 1。
历史 v38 replay 中 730 条 observation/player/object 记录与本轮原生观测逐行相同。

单次阶段计时样本：原始 replay 的 battle JSON parse 为 113.317 μs、GameState load
为 110.068 μs；注入路径分别为 98.858 μs、103.442 μs，52 条逻辑命令 parse 合计
15.542 μs、append 合计 1.002 μs。计时只用于辨认阶段成本，样本数量不足以构成
性能基线，也不支持原始 replay 与注入 replay 的性能优劣结论。

## Runner 变化

- 阶段计时通过 `NASHURA_PHASE_TIMING=1` 显式启用，默认关闭。
- 逐 tick 战斗状态观测通过 `NASHURA_BATTLE_OBSERVATIONS=1` 显式启用，默认静默。
- `--without-snapshot-trace` 关闭逐 tick serializer 归档，适用于轻量路径诊断；结果
  仍要求原生终局、Java 正常返回及 PM 零拒绝，摘要会标记未采集 snapshot trace。
- 不启用这些诊断开关时，runner 仍默认采集并校验 snapshot trace。

## 适用边界

本轮只覆盖一个固定 fixture 和顺序执行的路径诊断；没有进行并发测试或并发吞吐测量。
阶段耗时是单次 guest 观测，不属于之前的性能表。没有新增 system_server、
SurfaceFlinger、Display、GPU/EGL 或其他图形服务依赖。没有生成逐 tick 快照归档；
原始 wrapper 的空 controller、其他卡组/模式、未序列化全局状态、完整
ResourceManager 初始化语义及整体 ready 门仍待验证。

首次从用户目录加载新 observer 时被 ART linker namespace 拒绝；改用独立 `/dev/shm`
路径后运行成功。失败日志留在私有诊断目录，不作为本报告的成功证据。发布仅包含
脱敏结果和机器摘要，不含 observer 二进制、guest 原始日志、APK、DEX、libg 或 replay。
