# ARM64 组件检查目标 guard 快路径（2026-10-31）

状态：第一轮固定输入的真实串行正确性与性能门通过；候选默认关闭，独立确认待做，整体目标继续。

## 证据与实现

沿既有 hot-distance-v1 四组 warm-window perf 追踪，组件检查 +0x115d540..+0x115d604
叶样本分别为 429 / 502 / 180 / 198。常量 getter +0xf20210 的叶样本在保留 top12
调用链中分别有 71 / 70 / 27 / 20 条带组件虚调用返回 PC +0x115d5a3；采样返回 PC
与叶 IP 分开解释，不将这些数当作调用次数或全路径命中率。旧 profile 的“getter”标签
在此实际表示 mov w0,#5; ret 常量虚函数，不是字段 getter。

原函数先检查 x/y 的 signed bounds，再检查对象虚函数类型及数个纯字段 getter。
当 flag bit0 为 0 时，无论这些纯读取的结果，最终返回 1。新 component-guard-v1
保留全部原 bounds 指令，只把 +0x115d568 的栈保存改成直达近分支岛：

- bit0 为 1：立即回退原 prologue 与原函数余部。
- bit0 为 0、对象为空：直接返回 1。
- bit0 为 0、对象非空：读取当前 vtable slot +16，只有目标精确等于 engine+0xf20210
  才直接返回 1；其余目标回退原实现，保留潜在虚函数副作用。

这没有缓存虚调用结果，也没有跨迭代状态缓存。完整 196 字节函数正文与四个纯函数
正文均精确 guard；组合模式只接受既有 11 点 getter 替换后的明确三处差异。
原始 ELF 的可执行 PT_LOAD 逐条扫描未发现直接跳入被替换 prologue 字的分支。
该扫描不证明任意间接入口不存在；支持范围是正常函数入口和已验证调用点。

原先 bounds 让 x8 持有 grid；分支岛只用 caller-saved x9/x10，匹配路径不建栈、保留
来路 LR，回退精确重放 stp x29,x30,[sp,#-64]! 再回到 +0x115d56c。近岛分配器复用
既有 maps 空隙提示，只用非 MAP_FIXED 的 mmap，核对实际地址和双向 ±128 MiB 范围。
所有代码 RW→RX、刷新指令缓存，无 RWX；安装在首次战斗前的串行 Mainloop，库文件不变。

适用边界：完整有效的普通内存 grid/object/vtable/相关字段、串行稳定对象和代码。
省略的纯读取、故障地址/时序、signal/profiler 栈、MMIO/volatile 与并发变更不承诺等价。
未匹配的虚函数仍原样调用；不是无条件跳过类型判断或全部组件逻辑。

hot-distance-component-v1 显式组合原 11 点、两距离函数与本候选；不包含不稳定的
query pair-load、grid unroll 或 52 点 getter 扩展。所有候选仍默认关闭。

## ARM64 与全帧门

计时前后分别运行同一 SHA 的 ARM64 指令程序，各 120000 组：matched 4593、fallback
18433、null 4617、signed/bounds rejection 92357。对照执行精确原始函数和实际安装
分支岛，检查返回、x19..x29、grid/object/nested 未写入、带副作用类型函数和 obstacle
调用次数，保护页通过。这里是边界 harness，不代替真实完整局。

同一新 observer 上原始、hot-distance-v1、新组合，各四组完整局，共 12 局 34047 帧。
主机从归档重新解压、逐帧比较全部 serializer payload，逐行比较 observation/player/object，
全部与历史原始参考一致。每组 tick 为 3601 / 3601 / 2154 / 1989，固定命令数为
52 / 51 / 22 / 19。有限输出一致不证明全部内部状态或所有卡牌/动作等价。

## 正式串行测量

固定 VMware Fusion ARM64 Ubuntu，2 vCPU / 6144 MiB，kernel 6.8.0-138-generic；
同一 libg、DEX、资源、两八卡组、seed 1/7、固定非空动作。四组 case 顺序 a1/a7/b1/b7，
每组 ABBA/BAAB：combined/candidate/candidate/combined/candidate/combined/combined/candidate。
共 32 批×216局，剔除每批16 warmup，6912总局/6400测量局。每16tick采样、reset间轮转
相位；全帧门、工程检查、编译和诊断不与正式计时重叠。关闭 perf、Frida、REA/Ghidra、
strace、逐 tick snapshot、详细日志和阶段/线程诊断。计时只包含 warm step-loop，
不含冷启动、reset、首次加载和终态序列化；每局终态身份另行校验。

| case | 批次中位 ms：既有组合 → 新组合 | 耗时变化 | 聚合吞吐变化 | 两 block 耗时变化 |
|---|---:|---:|---:|---:|
| deck-a-seed-1 | 26.847307 → 25.967767 | -3.28% | +3.64% | -3.30% / -3.38% |
| deck-a-seed-7 | 28.923949 → 27.759600 | -4.03% | +4.33% | -3.66% / -4.65% |
| deck-b-seed-1 | 12.993282 → 12.577578 | -3.20% | +3.14% | -3.32% / -2.99% |
| deck-b-seed-7 | 12.398259 → 12.022531 | -3.03% | +3.22% | -1.97% / -4.49% |

batch median 的中位数为表中耗时；聚合吞吐为全部 measured ticks / measured seconds。
每 case 仅两个平衡 block，批内局数不作为 IID 重复，不作总体显著性声明。

采样 step 延迟 μs，各值为四批对应分位数的中位数，不是合并所有样本的总体分位数：

| case | p50 既有 → 新 | p95 既有 → 新 | p99 既有 → 新 |
|---|---:|---:|---:|
| deck-a-seed-1 | 5.4165 → 5.3848 | 15.4467 → 14.3113 | 20.5407 → 18.9780 |
| deck-a-seed-7 | 5.8145 → 5.7540 | 15.6925 → 14.4055 | 19.5053 → 18.1668 |
| deck-b-seed-1 | 5.4575 → 5.3125 | 8.8848 → 8.4780 | 10.1242 → 9.7490 |
| deck-b-seed-7 | 4.9785 → 4.9580 | 11.1445 → 10.4365 | 12.8837 → 11.9367 |

四组与八个 block 耗时均下降，四组采样 p95/p99 均改善。但 host 温度、频率和 VM
调度未锁定，尚未跨独立 session 确认，因此继续默认关闭；下一步用不变构建反转 case
顺序复验，再考虑延长同进程稳定性。不能将本轮增量与历史不同 session 的收益直接相加。

## 身份、工程验证与证据保存

- libg SHA：110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783。
- observer SHA：896d9f20cb415dacc0a1fb4149cd7fed80055ebc5a2024277864e4b5d1bd1892。
  从 repo 根、同一相对源码路径用 NDK27.3 Android30 -O2 -g -Wall -Wextra -Werror
  精确重建，cmp 相同；原始和两个组合均使用同一 observer。
- 私有原始归档：6792930字节，SHA
  6ec7aaa9fb4af90da97e2a3db534f1d2713c575355b1a291d28f5132e79bd953；309文件逐项身份通过，
  部署的251份 Python/fixture 文件与归档和主机源码逐项一致。后续新增静态审计工具与
  七项 decoder/拒绝检查仅在计时结束后运行，没有改变已测 observer/runner。
- 计时前 pure_linux 1454 passed/1 skipped；最终1461 passed/1 skipped；公开九套
  1104 passed/42 skipped；主工程211 passed；2974份源码清单、gitleaks与diff检查通过。工程门与真实局分开。
- guest仅剩约22MB时，重新核验主机副本SHA后清理两份query归档和一份tools tar重复副本，
  回升至37662720字节。原始实验目录、主机归档和失败证据保留；本轮未改VM配置。
- 准备期间一次客户端 JavaScript quoting 语法失败发生在执行 shell 前，未启动引擎；
  修正后执行，新实验数据没有替换或覆盖旧阶段。
- 公开仅保存自有源码、报告、哈希和脱敏统计，不包含第三方 ELF/APK/DEX/资源或私有原始日志。

硬件 PMU cycles/cache、完整在线动作分布、未序列化全状态、off-CPU原因与长期稳定性仍有
缺口；不扩张 Android 服务，不修改 main、冻结部署或2vCPU/6144MiB配置，不测试并发。
整体目标保持 active。机器摘要见 serial-arm64-component-guard-evidence-20261031.json。
