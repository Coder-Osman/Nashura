# ARM64 固定动作延迟分布诊断（2026-10-31）

状态：20完整局、56745帧及已观测状态与原始参考一致；16诊断局独立复算通过。
本阶段没有新增加速结论，整体warm运行时目标继续。

## 固定环境与实现

Fusion Ubuntu ARM64固定2vCPU/6144MiB、affinity[0,1]、kernel6.8.0-138-generic。
保持原libg/资源/DEX、A/B卡组、seed1/7和52/51/22/19固定非空动作。无并发测试，
不修改main、冻结部署、VM配置或外围Android服务；当前链路无QEMU指令翻译。

--action-distributions默认关闭，要求单局原始native-phases诊断，与benchmark、优化及
动态手牌记录互斥。诊断时step前一次分配4096条固定容量；逐动作不分配内存，
全部step结束后输出并释放。溢出以126拒绝进程，不截断样本。关闭时不分配采样缓冲、
不读取额外时钟或输出样本；既有parse/append和dispatch窗口原样调用原始libg目标。

parse/append复用已有计时边界；mode2/3复用native窗口耗时，额外在窗口前读取
原始GameState tick用于对齐。分布包含时钟/包装器与诊断开销；exclusive只扣
已插桩子窗口。工程检查与诊断执行存在重叠，主机热状态/频率/VM调度未锁定。
本轮保留逐tick serializer及关键状态，不能作为安静性能基线或加速比较。

inject的native parse/append在step前；cmd解析仍包含于GameState load。这里不是
在线动作输入，也不单列虚构cmd parser耗时。原始mode3目标固定+0xd30a00；
mode2本批零命中，空分布不填零成本。原始返回22每输入两次，其余0，不解释动作成功。

## 完整局与分布

先运行同一新observer原始基线各四局，再正向a1/a7/b1/b7及反向b7/b1/a7/a1，
每输入依次运行cmd和将相同命令移入inject的诊断。20进程严格串行、各引擎exit0、
Java正常返回、PM拒绝零。56745 serializer帧及已观测字段与既有原始参考一致。
同输入四个诊断局的dispatch tick/目标/原始返回序列逐项相等。
原始动作数不当apply数：a1/a7终局仍有两条未来命令，dispatch分别50/49，b1/b7为22/19。

两轮共1136个样本：parse288、append288、mode3 dispatch560、mode2零。
下表分位数均为单诊断局内部sorted floor((n-1)*p/100)，不插值，不将不同局pool或作IID。

| case / pass / path | dispatch数 | p50 μs | p95 μs | p99 μs | 最大 μs |
|---|---:|---:|---:|---:|---:|
| deck-a-seed-1 / pass-1-cmd | 50 | 9.500 | 19.584 | 21.208 | 27.750 |
| deck-a-seed-1 / pass-1-inject | 50 | 9.333 | 18.083 | 19.042 | 25.000 |
| deck-a-seed-7 / pass-1-cmd | 49 | 9.791 | 23.416 | 28.042 | 30.458 |
| deck-a-seed-7 / pass-1-inject | 49 | 9.667 | 23.625 | 24.958 | 26.459 |
| deck-b-seed-1 / pass-1-cmd | 22 | 9.375 | 11.750 | 11.959 | 25.250 |
| deck-b-seed-1 / pass-1-inject | 22 | 9.459 | 11.208 | 13.542 | 27.250 |
| deck-b-seed-7 / pass-1-cmd | 19 | 10.208 | 26.292 | 26.292 | 27.834 |
| deck-b-seed-7 / pass-1-inject | 19 | 11.000 | 27.291 | 27.291 | 33.250 |
| deck-b-seed-7 / pass-2-cmd | 19 | 10.708 | 27.291 | 27.291 | 31.084 |
| deck-b-seed-7 / pass-2-inject | 19 | 11.042 | 32.792 | 32.792 | 37.834 |
| deck-b-seed-1 / pass-2-cmd | 22 | 11.209 | 17.833 | 21.000 | 37.292 |
| deck-b-seed-1 / pass-2-inject | 22 | 10.834 | 17.958 | 22.208 | 23.250 |
| deck-a-seed-7 / pass-2-cmd | 49 | 10.959 | 24.084 | 28.541 | 32.458 |
| deck-a-seed-7 / pass-2-inject | 49 | 14.167 | 33.209 | 37.375 | 38.125 |
| deck-a-seed-1 / pass-2-cmd | 50 | 11.750 | 20.417 | 25.000 | 31.458 |
| deck-a-seed-1 / pass-2-inject | 50 | 12.417 | 22.917 | 26.750 | 29.167 |

| case / pass | parse数 | parse p50/p95/p99 ns | append p50/p95/p99 ns | append零时长数 |
|---|---:|---|---|---:|
| deck-a-seed-1 / pass-1-inject | 52 | 209 / 333 / 917 | 42 / 42 / 83 | 22 |
| deck-a-seed-7 / pass-1-inject | 51 | 208 / 250 / 791 | 0 / 42 / 42 | 28 |
| deck-b-seed-1 / pass-1-inject | 22 | 250 / 333 / 375 | 0 / 42 / 42 | 13 |
| deck-b-seed-7 / pass-1-inject | 19 | 250 / 375 / 375 | 42 / 42 / 42 | 6 |
| deck-b-seed-7 / pass-2-inject | 19 | 250 / 458 / 458 | 41 / 42 / 42 | 8 |
| deck-b-seed-1 / pass-2-inject | 22 | 291 / 416 / 417 | 41 / 84 / 84 | 9 |
| deck-a-seed-7 / pass-2-inject | 51 | 292 / 417 / 958 | 0 / 83 / 84 | 30 |
| deck-a-seed-1 / pass-2-inject | 52 | 250 / 542 / 916 | 0 / 42 / 84 | 31 |

append共147次零时长，受约41–42ns量化及计时开销影响；不是零成本。
dispatch分布随输入和运行顺序变化，不能从cmd/inject差异推导路径加速。
这些数据补足逐动作分布，下一步需独立设计在线输入和更细native apply路径。

## 独立复算、身份和异常

主机逐文件核验209实验文件、394冻结工具文件及控制脚本；归档9630399字节，
SHA 2bb6c97b6a9a1cfbe1adb10a23e50b5023facc766a39c8dc9973575f8d914562。
observer SHA 197d362c498d42c2dc7d899d0d879e773d61791c8c7057aab00c6fe9d540467c。
工具tar2795520字节，SHA 7876b92e7be378f193f2fb43b0e17c6b926b1ed85b3816452a581ac9448664a8。
NDK27.3.13750724 Android30 ARM64，-std=c11 -shared -fPIC -O2 -g -Wall -Wextra -Werror -llog -ldl。

summarize_headless_actions严格守恒逐项样本与既有累计计数、总时长、min/max、
零时长、返回零次数、目标变化、队列增量及顺序。host verifier从压缩原始日志与
快照独立复算20局、精确输入/环境/计划、前后时钟、全部帧与状态。13种独立
证据篡改在另一个复制目录被拒绝，恢复后完整图再次通过；原始归档保持。

启动包装器经vmrun shell多层引号使退出标记内容为n，原字节保留。没有将其
改为0或声称controller exit0。控制器PID130479已消失，engine进程为空，
20个逐局原生退出码0、20条结果与done/ALL_DONE完整图一致；控制器退出码仍未证明。

初次合成样本max与实际两条时长不一致、其次篡改替换同时修改expected和actual，
测试分别失败；修正fixture和精确匹配后123专项通过。保留v2/v3/v4工程log，
不将合成fixture作真实引擎证据。host编译buffer验证默认关闭、释放和4097th样本exit126。

最终pure_linux1550passed/1skip、公开1104passed/42skip、主工程211passed。
新增host验收器和后补测试/文档未部署，不把当前源码SHA冒作394文件冻结执行身份。
公开仅自有代码、脱敏统计和身份；第三方二进制、资源、原始逐动作记录与凭据保留私有。

## 后续

最新查询高度组合已另行完成双轮真实速度确认及132096局持续观察，本轮诊断不叠加
收益、不启用候选。继续在线动作apply、剩余对象/空间查询热点、分配同步、硬件
PMU缺口和更长时稳定性；不为补计数修改固定VM，不扩外围服务，整体goal保持active。
