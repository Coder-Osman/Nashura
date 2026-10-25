# 超时恢复工程完成，真实terminal续跑中

2026-10-25，父selection-1792730822491650000已停止并完整归档：原预算28800秒，
实际28800.344秒、exit124，无terminal报告。selected.pt保留第15步、7个完整
epoch、局内offset256、已消费2720行，下一批96行；该失败不改记为成功。

本轮实际重验父失败、冻结执行、数据身份及latest/best完整内容通过；结论仅为
`passed_timeout_archive_and_checkpoint_content_only`，live_resume_verified=false。
外部父launch SHA256：
`089fcec88dc04d02c326981ca35cfd7a055e87188581214ca173b8cc63007834`；
父receipt：`c2e22ea804d8aaa05215a05f6cb396181f8cc05488d7f7b391a8eede7edc7229`；
超时checkpoint：`5111d8c7077a05c0a987e4c40ba60975bea84e33533d51699637ea11bdba8937`。
原始报告含训练行游标，私有保留，不作为公开附件提供。

新增17项专项与既有26项选优执行回归联合43项通过，79.656秒，见tests-final.xml。
小模型第9步中途恢复至第24步与连续训练的完整状态摘要一致，涵盖模型、优化器、
RNG、采样与选优；另测试真实子进程参数贯通、父字节保持、隔离失败、活动进程/
租约不归档、重复收取幂等及篡改拒收。SSH/SFTP成功场景为显式模拟，不冒充
原生终局。tests-initial.xml保留初次fixture路径错误，tests-fixture-write-once.xml
保留测试错误覆写write-once request的失败；修正测试构造，未降低原生校验。

独立selection-retry-1792902961215570000实际在.35物理GPU1启动，预算86400秒。
worker73377/start49307405，训练子进程73450/start49307994；launch SHA：
`42c3a952d2adcb29977cf25893d0732a464bfaed56a302659790e6ec6976b7e5`。
快照1792903002确认两者活跃，命令显式携带上述第15步checkpoint SHA，冻结
确定性运行时已配置；当时尚未报告resume或新更新完成。预检V100 32GB可用，
可用内存239.288GiB、盘202.969GiB；预检不代替正式两代容量门。
恢复入口先重算best的358行验证，再恢复latest RNG/游标；此前一次完整验证
约2576.85秒，该耗时只作历史参考，不将长验证误报成重训或卡死。

Full128新24小时段native-search-segment-1792902074335182000仍运行：
快照1792902982已恢复174组、348根、44544有效模拟及354条记录至tick5728，
当前玩家0搜索中。passed_replay_only不代替完整终局；旧段保留归档。
独立global-search-1792836650700032000快照1792902971完成1/4根、仍运行，
目标四根各128模拟；逐根完整native+全局MT恢复及清理尚待独立验收。

G0/G1开放，限定子项仍34/43；G2–G8、正式训练与五机放量均未启动。
