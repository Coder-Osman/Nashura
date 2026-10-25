# Headless阶段交付

执行日期2026-10-26；仅任务分支，不修改main、不启动正式训练。

## 已提交阶段

| 阶段 | 状态 | commit | 结果 |
|---|---|---|---|
| P0审计 | IMPLEMENTED / UNIT_PASS | `0863e2e9ead51075dbc28f624db6148b30d70347` | 主线/历史范围核实，视频技术路线未核验，冻结任务只读检查 |
| P1原生可行性 | NATIVE_VERIFIED | `13f7890b32d527aa129416e68ee05df556786f84` | 两次原版完整局、26动作回执、29边界独立比较通过 |
| P2自写8卡MVP回退 | NOT_RUN | — | P1可行，条件未触发；已有sim_core不作为训练替代后端 |
| P3限定原生契约 | IMPLEMENTED / UNIT_PASS / NATIVE_VERIFIED | 本文件所属最终提交 | 拒绝/reset/快照通过，5tick分段两局与250tick推进的全部ordinary边界及终局一致 |
| P4重复吞吐 | IMPLEMENTED / NATIVE_VERIFIED | 本文件所属最终提交 | 1/2/4/8路各3轮，45局全部完整，逐轮指标公开 |

P1唯一决策：**NATIVE_HEADLESS_FEASIBLE**。它是QEMU ARM64 user-mode + 原版战斗逻辑 + Android Bionic/ART/JNI/资源，不是完整Android Emulator，也不是零Android依赖或宿主ISA直接执行。

## 实际验证

- `python tools/run_main_engine_checks.py`：最终复跑退出0，211 passed/22.04秒。
- `python tools/run_public_checks.py`：退出0；794 passed/27 skipped，另8组专项分别23、82、15/3 skip、25/4 skip、31/3 skip、31/2 skip、38/3 skip、65通过；范围不累加。
- `PYTHONPATH=firstlight_CR python -m pytest -q -p no:cacheprovider firstlight_CR/native_runner/tests/test_sim_core.py`：退出0，18 passed/0.28秒，只是原型基线。
- `python -m pytest -q -p no:cacheprovider tools/tests/test_headless_native.py tools/tests/test_headless_trace_verifier.py --junitxml=publication/headless-tests.xml`：退出0，27 passed/0.04秒；fixture不替代真实原生验收。
- `python tools/verify_headless_traces.py LEFT RIGHT --output NEW`：实际私有原生压缩轨迹，退出0，29边界，tick3601自然终局；输入与日志SHA见`publication/headless-p1-native.json`。
- 新版原生入口附加`--step-chunk 5 --protocol-check --concurrency 1 --repeats 2`：退出0，两次完整局与主基准ordinary轨迹及终局SHA相同，见`publication/headless-p3-native.json`。
- 主基准`--capture-mode ordinary --concurrency 1,2,4,8 --repeats 3 --observation-ticks 250`：退出0，45局全部完整、26动作逐局核对、拒绝0。
- `python tools/check_public_snapshot.py`、gitleaks、git diff检查分别记录于`publication/headless-release-checks.json`；清单随新增成果更新。

## 吞吐与正确性摘要

| 并发 | 完整局数 | 局/秒 | 摊销秒/局 | 单局均值/P95秒 | 扩展效率 |
|---:|---:|---:|---:|---:|---:|
| 1 | 3 | 0.1574 | 6.353 | 6.344 / 6.503 | 100.0% |
| 2 | 6 | 0.3004 | 3.329 | 6.553 / 6.816 | 95.4% |
| 4 | 12 | 0.5121 | 1.953 | 7.522 / 7.955 | 81.3% |
| 8 | 24 | 0.8025 | 1.246 | 9.145 / 10.066 | 63.7% |

8路相对1路总吞吐5.10倍，不是单局加速或Emulator倍率。每原生进程峰值约363MiB；全部子进程CPU核数等效0.892/1.680/2.898/4.579。启动均值约36.2–36.6秒，reset均值42.7–75.2毫秒。固定工程脚本不是FAIR搜索或训练吞吐。完整原始逐轮指标、标准差与样本限制见`HEADLESS_ENGINE_BENCHMARK_2026-10-25.md`。

原生限定检查：step0拒绝且状态不变、reset使旧快照失效、快照恢复及handle释放通过。本任务30条进程身份记录核验退出、监听残留0；冻结输入SHA复核不变。新Android逐机制对照为NATIVE_PARITY_NOT_RUN。

## 失败保留

1. 首次回执工具误将消费tick编码为t2+1，按已有原生消费契约修复为t2；不改引擎。
2. 原始JSON含跨局递增遥测ID，重复摘要失败；逐字段证明仅身份偏移后精确重定位，不删除战斗字段。
3. 轻观测只在终局读取完整事件时，首两条回执已离开窗口；工具拒收。修为每1000tick及终局抓取完整事件，保持全部动作回执验收。
4. ordinary新路径首个fixture测试捕获空事件摘要调用错误；已修复并补测试，旧尝试留存。
5. 富观测基准1/2路完成后人为中止本工具4路启动以切换测量口径；未向冻结任务发信号。两套口径不混为倍率。

## 路径与限制

- 代码：`tools/headless_native.py`、`tools/verify_headless_traces.py`。
- 使用与语义：`tools/HEADLESS_NATIVE.md`。
- 审计与路线：本目录HEADLESS_ENGINE_AUDIT/HEADLESS_ENGINE_PLAN。
- 公开摘要：`publication/headless-p0-checks.json`、`publication/headless-p1-native.json`、`publication/headless-p3-native.json`、`publication/headless-benchmark.json`、`publication/headless-rich-benchmark.json`、`publication/headless-cleanup.json`。
- 测试日志SHA：主工程`bd5c43c8b2aeb71fa342e528d94c3c7abc821bff74f52fb1dfcc3d558ee66bcd`；公开入口`d51a14f1cc930984826cadcc6ed62d0d957ababe2cf95e541c266c9875f9acb6`。工具及发布检查日志身份见`publication/headless-release-checks.json`。
- 原始二进制、资源、完整观测、机器/进程地址及私有对局均留仓库外。

当前证据仅固定工程卡组/seed/动作流；不是速猪全机制、所有卡池或完整FAIR搜索终局。未做本次Android同轨迹新对照，未产生Emulator相对倍率。G0/G1、34/43不增，G2–G8、正式迁移及自博弈训练均未启动。
