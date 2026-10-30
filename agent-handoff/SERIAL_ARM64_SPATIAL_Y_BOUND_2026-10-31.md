# ARM64 串行多卡组对照与空间查询单指令候选

日期：2026-10-31；分支：`codex/pure-linux-libg-headless-20261026`。

状态：`MULTIDECK_PARITY_PASSED_SPEEDUP_UNPROVEN`。整体目标未完成。
候选在临时进程内实际启用并通过四组完整局对照，默认关闭；稳定性能收益尚未证实。

## 正确性覆盖扩展

新增显式 `--record-hand-actions`，沿用已有原始 selection builder、命令 parse 与
append，每 100 tick 尝试两方首张手牌，记录精确 packed selection；禁止与 benchmark
并用，要求源 `cmd`、`inject`、`evt` 为空。`freeze_headless_actions.py` 校验正常退出、
终局、源 SHA、计划、两方非空动作、slot/card/成本/入队增长，原样冻结命令 JSON。

两组八卡集合为：

- A：26000000、26000001、26000005、28000001、28000000、26000003、26000014、26000018。
- B：26000000、26000003、26000005、26000021、26000024、26000014、28000000、28000001。

B 是实际更换卡集合。新卡 26000021 / 26000024 均由当前真实 catalog 的 selection
builder 选择并入队；固定回放的对象观察也包含 34000021 / 34000024 数据对象。
对象数量从初始六个增加至 11–14 个，不能仅以 queue 接受冒作动作已生效。

| 固定 case | 原始记录动作数 | 原生终局 tick | serializer 帧数 | 单份归档字节 | 观察对象峰值 |
| --- | ---: | ---: | ---: | ---: | ---: |
| A / seed 1 | 52 | 3601 | 3602 | 12,334,754 | 12 |
| A / seed 7 | 51 | 3601 | 3602 | 12,745,766 | 14 |
| B / seed 1 | 22 | 2154 | 2155 | 5,941,764 | 11 |
| B / seed 7 | 19 | 1989 | 1990 | 5,505,695 | 14 |

每个固定 case 先用旧 observer 独立运行两次，再用同一个新 observer 运行原始路径与
优化路径。三个配对均逐帧比较完整原始 serializer payload；0..final_tick 连续、帧长
和终点全部验证，四组无不同 tick。主机重新读取归档复算得到同一结果。
三条路径的对象、玩家、queue、终局等已观测状态逐行一致。

固定 fixture 在 `tools/pure_linux/fixtures/headless-runtime-deck-{a,b}-seed-{1,7}.json`。
动态入队记录与预加载固定命令的中间 queue 不作等价比较；后续仅比较同一固定输入。
serializer 适配器仍不包含原始客户端 controller，不覆盖未序列化的进程或全局状态。

## 候选及适用边界

前阶段 warm-window profile 的空间查询主体 `+0xf34b3c..+0xf35064` 占 16.68% 叶样本。
本轮核对精确 ELF 与 AArch64 指令：`+0xf34c78` 已跳过负 y；`+0xf34c84` 在
`height <= y` 时仍跳转到内层增量 `+0xf34c60`，反复检查其余越界 y。

候选只将这一条 `b.le` 的目标改到原有外层增量 `+0xf34c38`：

- 原始字：`0x54fffeed`；替换字：`0x54fffdad`。
- 不改对象过滤、输出次序、分配、标记清理或最终后处理。
- y 上界来自 32 位加法后算术右移 10 位，最大 2,097,151；尾部增量不会回绕到合法 y。
- 限定当前固定 ARM64 build、串行 Mainloop，查询期间 grid dimensions 稳定。

Java 原先已校验精确 libg SHA；安装器再验证函数入口、外层循环和分支周边字节，
在 Mainloop 首局开始前仅修改进程内四字节。页权限先 RW、再 RX，并刷新指令缓存；
不修改磁盘 libg，不修改 guest 内核或虚拟机配置。显式开关为
`--native-optimization spatial-y-bound-v1`，默认关闭；同一 observer 支持两个路径，
runner 还要求唯一的成功安装标记，防止旧 observer 静默忽略开关。

分支编码与单元格遍历模型测试只是局部工程验证；实际四组完整局和全帧配对才是
本轮真实对照证据。未证明其他 build、其他卡组/玩法或并发修改 grid 的语义。

## 同构建 A/B/B/A 测量

VMware Fusion，Ubuntu 22.04.5 ARM64，kernel 6.8.0-138-generic，固定 2 vCPU、6144 MiB，
affinity `[0,1]`。原始 libg SHA 为
`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`；
同一 APK/资源、DEX、ART/property 输入，新 observer SHA 为
`b04ffec2f8f6be02563fe17bba28ddd5262e9e6347e31f3c58a1d374d3452a66`。

每组按原始 A1、优化 B1、优化 B2、原始 A2 顺序串行执行。每批 116 局，前 16 局
warmup、后 100 局测量；共 16 批 / 1,856 局 / 1,600 测量局。每批独立进程，均正常
退出、PM 零拒绝，所有重复局的终态 tick、digest、字节数稳定并与对应正确性局匹配。

计时窗口只覆盖 warm step loop，排除 ART/冷启动、首次资源加载、reset、终态校验和
预热局。关闭 Frida、REA、Ghidra、strace、profiler、逐 tick snapshot、详细状态日志及
动态手牌动作。每 16 tick 抽样，起始相位在重复局间轮转。工程检查在全部计时退出后
才开始。主机频率、热状态与其他应用未锁定，保留 VM 调度和批次漂移边界。

| case | A1 / A2 中位 ms | B1 / B2 中位 ms | 两批中位均值变化 | A / B 合并吞吐 tick/s |
| --- | ---: | ---: | ---: | ---: |
| A / seed 1 | 28.536 / 28.909 | 28.923 / 28.409 | -0.20% | 125,320 / 125,076 |
| A / seed 7 | 31.089 / 31.428 | 31.264 / 31.132 | -0.19% | 114,995 / 114,993 |
| B / seed 1 | 13.870 / 13.788 | 13.617 / 13.776 | -0.95% | 155,361 / 157,309 |
| B / seed 7 | 13.542 / 13.360 | 13.362 / 13.224 | -1.17% | 147,868 / 149,388 |

变化列仅为 `(mean(B批中位) / mean(A批中位) - 1)`，不是置信区间或稳定加速。
吞吐按两批测量 tick 总和除以两批 step 窗口总时间计算；首组的吞吐略降，也说明只看
中位值容易误判。独立统计单位应为批次，每变体/每 case 只有两批；100 局重复有相关性。
**本轮不作稳定因果收益结论，不默认启用候选。**

完整逐批 step p50/p95/p99、跨局 spread、sample 数、RSS 和 timing 窗口记录在
[机器摘要](serial-arm64-spatial-y-bound-evidence-20261031.json)。step 分位数仍为各局
抽样分位数的中位数，不是 pooled step 分位数。

## 证据、失败与验证

- 四个动态记录、八个原始固定配对局、八个新构建原始/优化局与全部 timing 日志、计划、
  退出码及 serializer 归档在操作者私有归档保存；主机压缩包 11,503,680 字节，SHA
  `d7079df1715f4f2633d6fcf38b2b727abcb09d812c783d7bf6f62fd524a0b05f`。
- 初始候选部署的临时工具目录缺失，并遇到 PTY 单行长度截断；
  初始部署没有产出 battle 完成证据。保留初始部署痕迹；补齐目录后以新 v2 证据目录执行，不重启活跃任务。
- stdout 中整体 `HEADLESS_BATTLE_READY=UNVERIFIED` 保持原意。stderr 中既有 UnityAds
  类缺失与 storeKeyValue 例外发生在成功 battle step 窗口外；本轮不扩张外围服务。
- Python 3.13.5：pure_linux `1242 passed / 1 skipped`；公开九组合计
  `1104 passed / 42 skipped`；main_engine `211 passed`。新 ARM64 observer 使用
  NDK clang `-O2 -g -Wall -Wextra -Werror` 成功构建；全部 16 批使用同一个二进制。
- 公开源码清单 2,901 项、gitleaks 与 git diff whitespace 检查通过。
- 最终确认 guest 无 benchmark/profile/正确性进程残留；2 vCPU、内存与内核配置未改变。

本轮没有新 cycles/cache 硬件计数、action apply 独立归因、off-CPU 等待或长时间稳定性
结论。下一步继续定位合法格内候选过滤、动作与状态更新热点；多卡组配对作为后续
优化的回归门，不将本阶段正确性覆盖扩展缩写成整体目标完成。
