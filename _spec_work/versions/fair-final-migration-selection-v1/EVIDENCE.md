# 迁移轮次验证选优与恢复：限定工程验收

核对日期：2026-10-21。依据CONTRACT.md及冻结TRAINING_SPEC.md §4.3–4.4。
本轮仅收尾独立迁移接口工程；G0/G1保持进行中，G2–G8未运行。

## 最终验证

执行命令：

```sh
"firstlight_CR/.venv-live/bin/python" -m pytest \
  "_spec_work/versions/fair-final-migration-selection-v1/test_round_selection.py" \
  "_spec_work/versions/fair-final-migration-series-v1/test_series_training.py" \
  "firstlight_CR/native_runner/tests/test_fair_migration_trainer.py" \
  -q --maxfail=3 \
  --junitxml="_spec_work/versions/fair-final-migration-selection-v1/tests-final.xml"
```

最终106项通过、零失败/错误/跳过，XML耗时241.698秒。
范围为选优专项38项、既有跨轮训练37项、原迁移训练器31项；
这是一次联合运行的组成，不与历史111项或34个限定PASS子项累加。

- validation完整遍历、局内均匀/整局等权；不读取test、不优化验证行。
  编码器/教师参数及buffers、模块模式、全局RNG保持只读。
- fixture真实梯度训练覆盖冻结2轮、解冻阶段早停、基线参与选优、同值保留早者。
  工程强制早停配置patience=1/min_delta=1e6仅验证边界，并非正式超参数推荐。
- 选中模型与其对应AdamW、RNG、采样游标一起保留；测试故意使best不同于latest，
  并验证下一轮继承best。另以非空优化器moments完成两轮补充交接。
- 0/1/2/4/5/6次更新处恢复，下一批、指标、模型、优化器、选优历史及RNG一致；
  验证失败隔离当前运行，未终止父运行及错误历史manifest/回执前缀拒收。
- 16类损坏样本拒收，包含15类重新计算外层checksum的篡改及NaN旧checksum样本；
  best实时validation重算与记录一致。未逐一保存/重算所有非最佳历史模型。
- 独立CLI用fixture整局真实完成更新→原子保存→恢复→下一次更新，与直接更新对照；
  源checkpoint加载和资源bundle使用fixture替身，训练/数据预检/优化器/序列化为真实实现。
  该测试证明入口编排，不证明真实A加载或原生连续训练已验收。
- 恢复seed/batch/micro-batch不一致拒收；连续保存必须匹配上一个SHA，
  错误SHA或未显式允许覆盖时保持旧文件。原子替换并fsync目录仅面向单写入者，
  未声明多写入者租约、并发CAS或CPU/GPU跨设备逐值恢复。
- 选优模型导出前后固定fixture帧WDL和完整动作log_prob逐值相同。

## 保留的失败记录与修复

1. tests.xml：32通过/1失败，128.602秒。NaN篡改样本在测试构造checksum时即被拒绝，
   尚未进入恢复器；修正测试，使其实际调用恢复器。未以此声称生产实现有NaN接受漏洞。
2. tests-entrypoint-failed.xml：36通过/1失败，144.705秒。CLI第二次保存实际触发
   FileExistsError，暴露不可变写入器与连续checkpoint保存不匹配。
   独立新版补上显式旧SHA校验和原子替换；原底层存储/训练器未修改。
3. tests-final.xml为修复后的联合回归；不覆盖上述失败记录。

## 运行中原生局与范围边界

快照1792569413.7405872：原完整搜索attempt已提交18组、持久化tick724；
tick724双方128访问耗时650.655362/667.845729秒，tick784玩家0搜索中。
tick212双边跨进程恢复内部报告passed；worker及29731/29732仍存活。
尚无完整原生终局、最终独立验收或所属引擎清理结论。

244个运行中冻结生产Python文件逐项SHA零变化；本轮不修改原series版本或其证据。
中心/home余量139.091587GiB，距200GiB差60.908413GiB；保留原门槛与历史数据。

未接入旧采集器或正式轮次协调器，未做round2原生采集、原生连续补充梯度/恢复、
外部seed事前清单、正式1280局、WDL终局校准、非劣实战或搜索增益评估。
所有本轮工程权重training_admissible=false、formal_g2_pass=false；
不发布模型、不新增总门PASS，不把验证损失变化解释为棋力提升。
