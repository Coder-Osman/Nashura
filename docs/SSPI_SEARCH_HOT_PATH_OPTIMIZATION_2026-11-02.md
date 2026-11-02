# SSPI 搜索热点优化：静态机制编码与 FAIR 摘要

基于 main@4461fb05c956a17988f337ee2e862d154f9775b8，本轮针对 2026-11-02
搜索剖析中确认的两个 Python/模型热点，复用冻结推理期间不变的静态机制表示，并避免将 PLAY 合法性张量展开为嵌套 Python 布尔列表。没有改动 MCTS 模拟预算、时域、动作、随机流、FAIR 语义、原生捕获协议或训练损失。

静态 EffectSemanticEncoder 与 MechanicProfileEncoder 的结果仅在 eval() 且
torch.inference_mode() 下缓存。缓存版本绑定模块、参数和 buffer 身份、版本、
设备、精度及形状；训练/梯度模式、模式切换、设备/精度转换、权重载入和参数/
buffer 原地变更都会使缓存失效。梯度路径始终重新计算静态编码。

SSPI FAIR SHA 仍使用原有 semantic_digest 输入结构和字节协议。PLAY 的三维 bool
掩码以 tensor-backed list view 编码出与旧 tolist() 递归完全相同的摘要字节；
SHA 每次重新读取全部字段与 tensor 内容，不缓存外层摘要，因此原地改动和不经
PyTorch version counter 的外部写入仍会在下一次摘要中体现。

## 本机 CPU 微基准

机器为 Apple M4 Max、macOS 15.6，PyTorch 单线程 CPU。模型使用初始化 seed
20261103，相同权重；输入是仓库完整 V4 tensorizer 生成的固定 HOG FAIR 测试
fixture，plays 形状 [4,576,5]，FAIR SHA
5bc94882e9963f6afa032773d7bae9469c5e1efb81a99d13f5387aaf357a5fd8。
前向先预热，再交替顺序做7轮配对、每轮每版10次；摘要交替顺序做7轮，旧/新每轮各10次。

| 路径 | 旧实现中位数 | 新实现中位数 | 局部比值 |
| --- | ---: | ---: | ---: |
| 完整模型暖态前向 | 80.185 ms | 15.591 ms | 5.14× |
| FAIR SHA（每次重读输入内容） | 25.494 ms | 0.900 ms | 28.33× |

以上 fixture 是确定性工程输入，不是原生实局快照。微基准只证明局部路径耗时，
不外推完整搜索速度。完整策略/WDL 输出、合法联合分布、同 seed 动作采样及 RNG
状态在静态编码缓存开/关下完全相同；两轮策略CE/WDL CE反向传播的逐参数梯度、AdamW 更新
和更新后 logits 完全相同；checkpoint 权重、AdamW 状态恢复一致。SHA 字节和张量
版本变更刷新由定向测试核验。

## 原生搜索对照

复用已发布的专用单 Engine5 顺序搜索探针，在端口26794串行运行 root 与独立
belief world。每次运行先由本机状态确认 worker 空闲，然后配置自己的诊断局；
未停止/重启 AVD、adb或其他engine，没有改端口转发。基线工作树为
4461fb05c956a17988f337ee2e862d154f9775b8，优化工作树从此基线派生。固定
seed 20261103、CPU、persistent session-v1，基线和优化各复测两次，每根128次
FULL有效模拟。

| 指标 | 基线两次 / 中位数 | 优化两次 / 中位数 |
| --- | ---: | ---: |
| 根墙钟 | 57.780 / 57.397 / 57.588 s | 24.611 / 24.739 / 24.675 s |
| inference顶层阶段 | 10.867 s | 2.007 s |
| transition顶层阶段 | 33.578 s | 18.272 s |
| 实际belief native ticks / advance调用 | 2812 / 2812 | 2812 / 2812 |
| 有效模拟 | 128 | 128 |
| discarded edge | 0 | 0 |

两版model SHA相同（9e1751...ac445e4）、root FAIR SHA相同
（df7486...6457d6），完整32项访问分布SHA相同
（2467ae...f459a8），selected action均为 card 26000030、cell232、
delay slot2。每次 FAIR snapshot round-trip、独立 belief lease释放通过，原生
snapshot handles为0。step、observe-atomic、observe、snapshot-create、
release-snapshot、restore及other RPC次数完全相同。transition内的对手策略采样/
叶节点推理为嵌套计时，不与顶层阶段重复累加。两次对照的墙钟中位数从57.588秒
降至24.675秒，减少32.913秒（57.15%）。

这是一个固定随机开局root的重复对照；证明这条真实搜索路径受优化影响，不代表
多root总体速度、训练收益或模型强度。原生计数、分布、SHA、各次stage与RPC数据
见[完整机器证据](SSPI_SEARCH_HOT_PATH_NATIVE_CONTROL_2026-11-02.json)。
