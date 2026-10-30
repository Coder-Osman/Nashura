# ARM64 组合候选之后的 warm-step 热点（2026-10-31）

状态：四组固定输入的真实 perf 采样与主机独立复算通过。只作热点归因，没有新增优化或速度收益结论；整体目标继续进行。

## 环境与测量

基于 d3b606766a71caa2d26f793422da5de8ed49b92e，沿用 2 vCPU / 6144 MiB Fusion ARM64 Ubuntu、同一 libg / DEX / observer、两卡组和 seed 1/7 非空固定动作。hot-distance-v1 显式启用，候选仍默认关闭。没有修改 VM 配置、冻结部署或外围服务，也没有并发运行实验。

每组一个进程、216 局，剔除 16 warmup，仅筛选后 200 局的 CLOCK_MONOTONIC [start_ns,end_ns) warm-step 窗口。perf cpu-clock:u、997 Hz、frame-pointer callchain；perf 获得必要权限，workload 经 setpriv 降回 uid/gid 1000。关闭逐 tick trace、详细观察和阶段/线程资源诊断。采样与前一阶段无 profiler 正式速度对照分开，采样耗时不计作新基线。

## 样本覆盖

| case | 全部样本 | warm-step 样本 | Mainloop libg 叶样本 | 每测量局样本 min–max | 丢样 |
|---|---:|---:|---:|---:|---:|
| deck-a-seed-1 | 7178 | 5414 | 5359 | 26–31 | 0 |
| deck-a-seed-7 | 7564 | 5806 | 5737 | 28–30 | 0 |
| deck-b-seed-1 | 4149 | 2638 | 2603 | 12–17 | 0 |
| deck-b-seed-7 | 3969 | 2494 | 2467 | 12–13 | 0 |

首组窗口还含 main 线程 1 个样本，其余组均为 Mainloop；不将全部窗口样本伪称为 Mainloop 样本。libg 叶样本占全部测量窗口样本约 98.67%–98.98%，不据此排除 off-CPU 等待。

## 剩余热点

以下为函数范围内的 Mainloop/libg 叶样本占全部测量窗口样本的比例；名称仅描述既有指令与诊断行为，未恢复原始类名。

| case | 空间查询 | 对象更新 | 组件更新 | 网格清理 | 指针距离 | 标量距离 | 字段 getter |
|---|---:|---:|---:|---:|---:|---:|---:|
| deck-a-seed-1 | 18.66% | 8.05% | 7.92% | 3.01% | 4.64% | 0.65% | 1.63% |
| deck-a-seed-7 | 19.26% | 9.01% | 8.65% | 1.96% | 5.41% | 0.88% | 1.50% |
| deck-b-seed-1 | 20.17% | 6.37% | 6.82% | 2.92% | 3.68% | 0.80% | 1.48% |
| deck-b-seed-7 | 18.85% | 8.98% | 7.94% | 2.85% | 4.61% | 0.84% | 1.16% |

## 函数范围与补丁边界

精确原始 ELF 的 GNU_EH_FRAME 索引及 FDE 双重核对函数起点/终点，原始函数字节 SHA 保存在机器摘要。当前只支持已确认的固定展开编码；错误 SHA、编码、索引顺序或 FDE 范围均拒绝。可执行 PT_LOAD 的文件偏移与虚拟地址在这些范围一致。

| 行为标签 | 原始范围 |
|---|---|
| query | 0xf34b3c–0xf35064 |
| grid-clear | 0xf34864–0xf348d4 |
| object-update | 0xf66e30–0xf672b4 |
| component-update | 0x115d540–0x115d604 |
| getter | 0xf20210–0xf20218 |
| pointer-distance | 0x121b860–0x121b8bc |
| scalar-distance | 0x121b8bc–0x121b910 |

对象更新和组件更新范围包含上一阶段的 getter 调用点替换，两个距离范围为替换后的进程指令；原始 ELF SHA 仅定位固定 build，不能把这些 IP 按旧正文解释。安装标记、相同 observer 身份与完整局全帧参考独立校验，未将采样混作新正确性门。

网格清理热点 +0xf348a8 对应逐 cell 清零计数；当前证据不足以省略清理或引入状态缓存。空间查询 +0xf34b3c..+0xf35064 仍占 18.66%–20.17%，优先审查循环边界与候选过滤；其次沿对象/组件的已证实调用链。已有 component-getters-v1 增量不一致，不因 getter 再次出现在样本中就提升为默认。

## 验证与限制

verify_combined_profile.py 重新读取四组压缩 perf 数据、原始 stdout、计划、精确外层命令、全部退出码及环境，校验安装标记、逐局固定命令数、终态、已有全帧参考与派生范围统计。九项合成检查通过；pure_linux 1401 passed / 1 skipped。公开检查 1104 passed / 42 skipped，主工程检查 211 passed；三套均正常退出，结果在机器摘要中登记。

私有原始归档 1801874 字节，SHA：cd8a030a428e2b095d694af316d526d213be7af7e64e36374bfa58926349d03f。原始第三方 ELF、perf 数据、完整日志和资源只保留本地；公开仅存自有验证代码、身份与脱敏统计。

每组仅一次 profile。叶样本比例不是函数时延、cycles、cache miss 或因果加速；callchain 返回 PC 与采样 IP 分开解释。当前 guest 的硬件 PMU 计数仍未取得，本轮没有新 cycles/cache 或长期无泄漏结论。
