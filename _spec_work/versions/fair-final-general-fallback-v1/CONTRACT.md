# 一般历史退化路径：独立工程版本

依据冻结 ACCEPTANCE_GATES.md G1 的 FAIR 非干扰、完成决策边界要求，以及
G0_G1_CLOSURE_CHECKLIST.md 的一般 pending、局部重条件化退化路径缺口实施。
仅依赖 history-v2/project；不修改该冻结副本、主项目或运行中的部署。

GeneralHistorySearchProvider 组合现有 RefillLocalSearchProvider 和 HistorySearchProvider。
优先独立增量锚点及局部未见牌重排；仅当 conditioner.apply 明确抛出
UnsupportedLocalBoundary，且原局部 provider 已成功清理，才从 typed FAIR 历史
独立初始化并有界重演。factory、同步、恢复、清理阶段的同名异常不触发退化。
协议错误、超时与无兼容世界继续失败；不读取真实比赛状态或真实私有种子。
每次历史构造默认4次尝试，允许1–32次，沿用全程 deadline，不增加外层重试。

一般路径每根重新构造，可能随历史变长；不宣称恒定成本或精确后验。
后继根可重新启用局部路径。成功历史前缀固定，回退、错玩家、错帧、错 tick
在工厂访问前拒收。一次仅允许一个 lease，重复 close 幂等，释放失败封闭生命周期。
组合 manifest 与 commitment 独立生成，不修改底层 world 的 manifest/commitment。
sampler 身份绑定工厂、两路径源码、选择规则、尝试预算与独立锚点种子承诺。

本版本仅工程验证，training_admissible=false，formal_g2_pass=false。
fixture pending 不证明原生合法可达。原生 pending/拒绝、总体先验校准、完整
Full128 搜索终局及独立恢复验收仍需分别完成，不据此关闭 G0/G1。
