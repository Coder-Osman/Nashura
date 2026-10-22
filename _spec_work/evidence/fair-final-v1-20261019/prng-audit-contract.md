# 原生 PRNG 非干扰诊断契约

## 覆盖与身份

只审计本次独立比赛进程的 GameStateManager 内嵌随机状态，不替换引擎二进制，
不改 source A、策略输入、信念种子或正式训练配置。

- libg SHA256：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- resident SHA256：`0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807`。
- 权威规则：冻结规格 ACCEPTANCE_GATES.md G0/G1 FAIR 非干扰条款。

## 静态定位证据

在上述固定 libg 中，直接解码 AArch64 指令，而非猜测偏移：

1. `0x1062a1c`：`add x0,x0,#0xa0`，随后跳至 `0x121b350`。
2. `0x121b350`：读取32位状态，执行左移13、算术右移17、左移5的 XOR 更新，
   保存状态后对绝对值取模返回随机结果；零状态先换为 `0xffffffff`。
3. `0x1063ba4`：恢复路径将 `manager+0xa0` 传入 `0x121b3ac`。
4. `0x121b3ac`：从输入流读出32位数，再写回收到的状态地址。
5. libg 中还有其他随机对象；本诊断不声称覆盖它们。
6. 原生tick getter `0x1062bc0` 读取 `manager+0x60`，不使用恢复路径的
   `manager+0x1bc` 临时字段；Android tagged manager指针按唯一可写映射解析。

## 隔离与审计路径

`NativePrngAudit` 仅在 tools 中使用，不导入生产策略、信念或训练模块。
它校验本次实例 process.json、PID启动时刻、工作目录、端口、QEMU命令身份，
以及两个库的完整SHA；resident全局变量地址从该库符号表读取。
地址映射要求精确文件路径和唯一零偏移映射，并按实际映射计算guest偏移。

进程暂停后确认所有线程停止，检查 generation/stateEpoch/tick 和空闲原生执行线程，
同时核对manager内tick；只推进 `manager+0xa0` 的4字节随机状态一次，
要求 expected-state 匹配并读回验证，最后恢复本次子进程运行。
旧PID、身份不符、状态不符均拒绝；调度器兜底恢复并停止自己的进程，
不修改系统 ptrace 设置，不接触其他任务进程。

## 预注册验收

- 先独立 smoke：实际随机状态/digest改变，原始可观察输出不变；双方FAIR帧、
  完整条件概率、候选、选择和WDL一致；快照恢复原始随机值与digest。
- 拒收 stale generation/epoch/tick/expected-state，并确认拒收后状态未变。
- Full阶段：开局100／后段3600阈值×双方座位，共4分片8根，
  每根128有效模拟、默认6组/200tick软视野和6组/60tick完成边扩展。
- 每对固定独立belief seed与执行采样seed，候选、N/Q、选择、标签、
  FAIR history、world commitment和逐模拟完成边轨迹完全一致。
- 搜索前后真实比赛digest及PRNG状态不变；快照、世界lease和所属进程清理。
- 没有完成Full原生验证和严格证据校验前，PRNG搜索子项保持IN_PROGRESS。

## 不涵盖

此处不是精确条件后验证明，不覆盖其他随机对象、中段/加时PRNG搜索、
合法单方ready信念根、一般经验先验、恒定开销重条件化或完整局搜索质量。
原生检查和Full结果都不构成正式训练授权；G0/G1总门槛保持IN_PROGRESS。

## 实测结果

`native-prng-smoke-report.json`和`native-prng-full-report.json`均通过严格校验；
Full四片8根/1024有效模拟完全一致，所有所属引擎已清理。
分别16/26类篡改负例被拒收。所选tick152和3602的原始manager PRNG值相同，
所以结论限定为该隐藏对象扰动的非干扰，不扩展为所有战斗随机对象覆盖。
