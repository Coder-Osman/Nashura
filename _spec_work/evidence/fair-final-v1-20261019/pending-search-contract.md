# 待执行计划下的完成边界搜索：内存模拟验收

本项使用测试用 TickEnvironment，通过模拟原生拒绝事件产生单方 ready，
对方保留 WAIT 或 ACT 预约；不直接改写 ready/pending，不发布训练数据。
它覆盖搜索运行时的完成边界逻辑，不等同于原生独立信念构造通过。

## 覆盖

- 双座位 × 对方 WAIT/ACT × 己方 WAIT/提前 ACT/同 tick ACT，共12例：
  未唤醒的对手不重新采样；错峰到期才采样；公开 ACT 抢占；同 tick 先结算。
  快照重复续演核对时钟、FAIR、执行轨迹与双方执行 RNG，结束释放快照。
- 双座位 × 三个 tick/决策硬限，共6例：未完成边回滚，不产生叶值。
  到达硬限 tick 后可处理刚唤醒对手的提交，但不得再推进越过硬限。
- 双座位 × 胜/平/负，共6例：终局使用根玩家视角，终局清空 pending，
  不因行动事件次数翻转价值。这里的终局证据来自测试替身。
- 双座位 × 对方 WAIT/ACT × Full128/Fast16，共8例：单世界构建一次，
  预算精确、无无效模拟及未完成回传；Full 标签总访问128，Fast无策略标签。
  叶值只在根玩家完成边界求值；关闭搜索后时钟、FAIR及执行 RNG 恢复。

32个新用例与相邻 single_ready/search/history_provider 回归合计106项通过。
各历史测试集合存在重叠，不与其他 XML 数量求和。

## 复现

在工作区根目录运行：

```sh
"firstlight_CR/.venv-live/bin/python" -m pytest \
  "firstlight_CR/native_runner/tests/test_fair_final_pending_search.py" \
  "firstlight_CR/native_runner/tests/test_fair_final_single_ready.py" \
  "firstlight_CR/native_runner/tests/test_fair_final_search.py" \
  "firstlight_CR/native_runner/tests/test_fair_final_history_provider.py" \
  -q --junitxml="_spec_work/evidence/fair-final-v1-20261019/p1-pending-search-tests.xml"
```

总门槛校验106条唯一用例、零失败/错误/跳过及四组新增覆盖数量，
并把测试源码、XML和本契约加入 manifest。

## 边界

不覆盖原生 pending 根的独立历史重建、局部重条件化、原生 Full128、
完整局搜索或训练准入。现有 refill provider 仍只支持双方 ready、无 pending。
G0/G1整体保持进行中，training_admissible=false；后续应在隔离原生诊断中
验证搜索接线，并单独解决拒绝历史与一般 pending 的信息边界。
