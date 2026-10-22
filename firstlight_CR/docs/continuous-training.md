# 持续 RL 训练

用户要求当前训练持续运行，不再设置训练轮数上限。持续模式使用显式空值，不用超大的轮次或未来时间模拟无限训练。

权威配置为 `.34` 当前 run 的 `fleet.json`：

```json
{
  "training_mode": "continuous",
  "target_iteration": null,
  "additional_iterations": null,
  "duration_seconds": null
}
```

恢复时保留原始 `started_at`，将 `schedule.json` 的 `deadline` 和 `duration_seconds` 设为 `null`，因此旧时间截止不会重新生效。持续模式拒绝同时配置轮数或时长上限。

每轮继续执行现有采集、回执核验、PPO 更新、归档及缓存维护。手动 STOP、训练异常和原有磁盘空间保护仍然生效；不自动忽略错误，也不自动重用未核验的半轮数据。

此次切换仅修改调度入口与预算配置。在完整轮次归档后暂停旧入口，从该轮 checkpoint 连同 AdamW 状态恢复；模型、采集规模、混合历史对手比例、PPO 参数和独立评估关闭状态沿用现有配置。

本地审计资料：`artifacts/continuous-training-20260920/`。远端审计资料：当前 run 下的 `continuous-training-20260920-audit/`。新日志命名为 `training-continuous-*.log`，检查训练日志时应同时兼容此前的 `training-target-*.log`。
