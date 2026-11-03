# 塔血启发式共享主体拟合诊断

> 本文保留最初v1诊断设置。用户随后要求每批新数据只训练8轮，当前v2执行与换批规则以[新数据八轮协议](SSPI_TOWER_FRESH_BATCHES_2026-11-03.md)为准；旧v1已在第11轮边界暂停。

## 变更授权与边界

仅价值头预热未出现有效拟合后，用户明确要求解除主体冻结、持续训练直至出现拟合迹象，并授权复用旧的8,171个训练状态。

本诊断从同一份 `seed=20261103` 随机初始模型开始，使用新的运行身份及新建AdamW。原始模型SHA为 `14fbd5fa43ba84be860f4333f3c63a935879b2ca0d6221978100c93cb6061415`。保留旧实验全部证据，不覆盖其失败结论，不启动原生采集或SSPI代际训练。

## 数据与更新范围

- 原32局训练的8,171状态全部参与每个epoch，各一次；原8局验证的1,996状态只用于评估和选择checkpoint。
- 这些状态来自原有50%监督采样，不是32局中所有16,390个真实决策。未落盘的决策不补造输入。
- 导出并校验原始完整FAIR张量、手牌输入、公开塔血目标、episode及FAIR身份；共享主体每个microbatch重新前向。禁止复用旧512维冻结共享表示缓存。
- 旧8局留出已被评估，本轮既不加入训练，也不重新宣称为独立留出验证。
- 共享主体6,923,203参数、价值头131,842参数参与更新，共7,055,045；策略头814,600参数冻结且排除在优化器之外。总参数7,869,645，架构不变。
- 损失仍为纯塔血差软标签CE，WIN/LOSS顺序与公式不变；不使用真实胜负损失、策略损失或搜索目标。

## 固定训练设置

AdamW `lr=1e-4, weight_decay=1e-4, betas=(0.9,0.999), eps=1e-8`；有效batch256，microbatch16，clip1。共享主体解冻后采用既有value-only的学习率，单一路径训练，不进行超参数扫描。尾batch按实际样本数归一化。

每epoch按固定seed确定性打乱全部训练状态。CUDA确定性算法开启、TF32关闭；初始state_dict由原Mac权重导出，避免跨平台重新初始化产生身份差异。不继承旧优化器和MPS随机状态。单GPU运行；模型、AdamW、CPU/Python/CUDA随机状态一并保存及恢复。

每轮评估全部训练与验证状态；验证按局平均MSE改善超过1e-6时保存最佳完整模型及优化器。保留latest用于整轮边界恢复，进程锁防止重复trainer。没有3轮无改善早停、没有epoch或时间硬上限；非有限值、身份校验失败等真实错误暂停并保存回溯。

## 拟合迹象的诊断终点

连续3个epoch满足以下条件，且验证MSE最佳checkpoint也满足时，恢复最佳模型与优化器并结束本轮诊断：

1. 训练和验证按局平均MSE均较各自两个常数基线中较好者降低至少50%；两个基线为恒零评分和训练集平均评分。
2. 验证按局平均MAE≤0.05。
3. 验证在绝对塔血评分≥0.05状态上的按局平均方向准确率≥80%，覆盖至少4局、100状态。
4. 主体和价值头均确有梯度和更新；策略头及静态缓冲区SHA保持；checkpoint模型/AdamW往返一致。

该终点用于回答“解冻后是否出现拟合迹象”。验证集被反复用于训练监测和选择，因此结果不是新的独立泛化验收。报告MSE、MAE、相关性、方向及按整局重采样区间，不将重复决策点当独立样本。

策略头参数冻结并不保持策略函数不变；共享主体更新后，固定实际状态的WAIT/PLAY模式概率和总变差单独记录。不以启发式拟合推断真实胜率或60%对战胜率。

## 运行与校验

```sh
PYTHONPATH=firstlight_CR python -m native_runner.training.sspi.tower_shared prepare \
  --source-run SOURCE_RUN --destination DATA_DIR
PYTHONPATH=firstlight_CR CUDA_VISIBLE_DEVICES=0 CUBLAS_WORKSPACE_CONFIG=:4096:8 \
  python -m native_runner.training.sspi.tower_shared_probe \
  --data-dir DATA_DIR --output-dir PROBE_DIR
PYTHONPATH=firstlight_CR CUDA_VISIBLE_DEVICES=0 CUBLAS_WORKSPACE_CONFIG=:4096:8 \
  python -m native_runner.training.sspi.tower_shared run \
  --data-dir DATA_DIR --run-dir RUN_DIR --run-id RUN_ID --device cuda:0
```

`STOP_AFTER_EPOCH`文件只用于请求在完整epoch边界暂停；存在时恢复不会继续更新。首次预检须在完整数据传输和校验之后执行。训练使用独立冻结源码，整套training Python文件参与源码SHA。

工程检查覆盖批量/逐条数值与梯度一致性、所有共享模块更新、策略头零梯度及SHA、尾batch归一化、模型/AdamW恢复后下一轮逐位重放，以及拒绝旧共享表示缓存协议。CUDA probe额外使用16个真实FAIR状态核对CPU/CUDA数值、真实更新、CUDA随机状态及恢复后下一步逐位一致。probe中单次耗时不作为完整后端性能比较。
