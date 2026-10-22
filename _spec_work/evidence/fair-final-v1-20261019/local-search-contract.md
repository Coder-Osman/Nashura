# Settled-root 局部补全 Full128 接线验收

## 固定范围

- 使用独立 phase-snapshot 新库，SHA256 为
  49098dbf5abe6a3e01dab5042f4f8c903337ef9a76e4cefa7808c4df013095ba。
- 中段、后段各两个座位；每片初始根及下一个双方 settled 根，均做实际隐藏
  牌序干预前后两次搜索，共16根、2048次有效模拟。
- 每根一个独立信念世界、128有效模拟、默认6决策组/200tick软上限及
  既有完整边扩展规则。根噪声0；策略与价值模型不更新。
- actual、anchor、search/behavior RNG 分离；搜索只接收 FAIR 历史及信息帧。
  真实比赛 seed 与隐藏干预证据仅存探针审计，不进入 provider 接口。

## provider 生命周期

同一 provider 保留独立冷构造锚点，每根以搜索 seed 局部补全隐藏牌序。
搜索世界结束后恢复锚点；重复 close 不影响后续 lease；同一时刻至多一个 lease。
构造、搜索世界释放或锚点恢复失败后，封闭 provider 并释放 session/transport，
不允许疑似损坏的锚点进入下一根。新增专项测试包含这些故障路径。

## 原生证据

每次搜索 root restore 核对原生 digest 和逐实体 phase 字段。
实际世界的 root restore 同样核对 phase，不只依赖未覆盖 phase 的旧 digest。
同根固定搜索/执行 RNG 下，干预前后候选、N/Q、标签、执行动作和完整模拟
轨迹必须一致。每根搜索世界释放、锚点保留；结束时锚点与全部引擎均回收。

后继根须显示 incremental，历史推进仅为两根 tick 差；自然发生重建时保留
失败证据，不将其标成增量通过。局部补全自身 history_ticks_replayed 必须为0。
原始记录、程序/库/来源文件哈希、端口和清理证据均由独立校验器核验；
负例包括预算、身份、隐藏干预、phase恢复、锚点来源、后继增量和清理篡改。

## 结论边界

这属于 settled 双方ready、无pending、手牌补齐的工程子集。未覆盖一般单方
ready、pending/补牌中途、跨slot遥测、完整局策略质量和吞吐，不宣称所有
非搜索开销已O(1)。冷锚点仍需独立历史重建一次。始终 training_admissible=false。
正式采集、训练和发布需后续门槛，不由本探针自动启动。
