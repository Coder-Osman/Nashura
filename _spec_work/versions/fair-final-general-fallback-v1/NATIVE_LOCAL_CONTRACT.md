# 组合 provider 的原生 local 路径验证

依据冻结 G1 的信息非干扰、完成决策边界和恢复要求，补上本组合 provider
此前只有内存 fixture 的部署证据。原 general_provider.py、冻结 history-v2
源码及正在运行的 Full128 部署均保持原字节。

run_native_general.py 独立部署冻结 history-v2 源码和组合 provider；
复用既有真实轨迹、phase-snapshot 引擎、资源保护和双引擎清理 worker。
worker 唯一替换为独立 probe 入口；原有探针与历史结果不覆盖。
专属端口29771/29772，启动至少64GiB可用内存、5GiB磁盘，运行保留32GiB。
工程资源预检不替代正式中心200GiB及两代实际容量门。

probe_native_general.py 将组合 provider 接入已验证 refill 原生探针，审计
适配只暴露底层 local 锚点句柄，不改变组合 manifest、sampler 或搜索逻辑。
注册根为tick110及下一个真实决策边界，分别对原始和改变真实隐藏牌序的
局面执行128次有效搜索；固定搜索与执行随机流、无根噪声。
每次核对真实比赛未改、模拟根恢复、phase状态、租约释放、增量锚点与模型不变。
不以内部世界身份作为信息键或选动作输入。

verify_native_general.py 独立核对部署/载荷SHA、来源模型、完整FAIR历史、
独立抽牌种子链、局部未见牌排列、外层组合身份、128访问与未完成边零回传、
执行采样、转移边界、隐藏配对和所属双引擎清理。缺失或失败报告拒收。
测试中构造的组合manifest及128条WAIT转移仅为验收fixture，不产生原生结果。

本探针只证明组合的local分支。遇到一般fallback即失败，不注入非法动作
制造pending，不将有限轨迹未发现拒绝解释为全局不可达。原生pending、
fallback、总体先验校准、完整搜索局和G0/G1整体仍分别待验。
所有结果training_admissible=false、gates_closed=false。

```sh
PYTHONPATH=_spec_work/versions/fair-final-history-v2/project:_spec_work/versions/fair-final-general-fallback-v1 \
  python _spec_work/versions/fair-final-general-fallback-v1/run_native_general.py
python _spec_work/versions/fair-final-general-fallback-v1/verify_native_general.py ATTEMPT
```
