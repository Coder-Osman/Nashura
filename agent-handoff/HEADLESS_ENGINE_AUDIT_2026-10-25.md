# Headless 引擎审计

执行日期：2026-10-26。工作分支 `codex/headless-engine-plan-20261025`。

## P0：版本与隔离

- 交接文档起点：`1352bf0245aca4fe14851595fed68724602c08bf`。
- 执行前 main/origin/main：`0012201830cfbfcbfeb3d9dccd91a855028da869`，工作树干净。
- 原发布目录：`/Users/gujiga/Documents/ChatGPT/CR/Nashura`。
- 独立任务工作树：`/Users/gujiga/Documents/ChatGPT/CR/Nashura-headless`。
- 仓库外源：`/Users/gujiga/Documents/ChatGPT/CR`；私有资源只读，不纳入发布。
- 五个计算节点已只读检查；四节点分别存在28/30/18/18个 QEMU user-mode 进程，全部视为受保护任务。一个节点未发现该类进程，仍不据此终止其他进程。诊断只使用独立新目录、独立进程及空闲本地端口。
- 本次只读核验：Full128最新分段completion的continue退出0、clean=true，进度187组/tick5998；不等于完整搜索终局。terminal选优留有exit124/28800秒的failed_quarantined记录，其状态文件的旧PID不是当前存活证据。未对两条链执行恢复、停止或清理。
- G0/G1、34/43保持开放；G2–G8、正式训练及多机放量均不启动。

## 仓库审计：IMPLEMENTED / UNIT_PASS

以下文件同时存在于当前 main 与历史隔离副本，不属于“仅历史原型”：

| 能力 | 当前路径 | 结论 |
|---|---|---|
| 8卡原型 | `firstlight_CR/native_runner/sim_core/` | 已包含 state/engine/env/ruleset |
| 原生客户端 | `firstlight_CR/native_runner/cr_native_env.py` | 有真实对局、推进、原子观测及快照接口 |
| 训练环境 | `firstlight_CR/native_runner/battle_env.py` | 继续作为训练契约来源，不改默认后端 |
| 隔离副本 | `_spec_work/versions/fair-final-history-v2/project/native_runner/` | 单独冻结，不用其测试代替主线测试 |

基线：`PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=firstlight_CR /opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider firstlight_CR/native_runner/tests/test_sim_core.py`，退出0，18 passed，0.28秒。

主工程入口复验退出0：211 passed，52.24秒；首轮211 passed/24.13秒的测试本身成功，但外围zsh脚本使用只读变量status而返回1，保留日志并已用result_code修正调用。公开完整入口同样重跑以取得可靠外层退出码。source-manifest基线2329文件通过；gitleaks扫描32.06MB零发现。

原型限制：简化字典观测非完整训练schema；缺 is_terminal/export_trace/match_config；位置被clamp；没有已证明的完整延迟/计划契约；seed尚未驱动洗牌。需做规则级对照，不能称原生等价。

## 视频路线：VIDEO_TECHNIQUE_UNVERIFIED

已实际访问指定视频页面、公开元信息、player接口和评论接口。标题为《全球3100名，我如何训练皇室战争AI击败人类高手？》，作者Jasonmoonw，时长1603秒。公开简介为空，匿名字幕接口返回need_login_subtitle且无字幕。

评论中有作者关于scaling/搜索的简短回复，也有观众的代码片段和内存读取推测；这些不证明作者引擎、QEMU、时钟或吞吐实现。尚未获得可核验的作者实现或对应技术段落，因此不采信视频为本项目技术依据，也不由评论推断完整实现。该缺口不阻塞工程验证。

## 原版 ARM64 静态依赖

匹配libg.so SHA256：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。LIEF实际读取为AARCH64 ELF，导出JNI_OnLoad。

DT_NEEDED：libscid_sdk.so、liblog.so、libandroid.so、libEGL.so、libfmod.so、libfmodstudio.so、libdl.so、libsentry-android.so、libsentry.so、libm.so、libc++_shared.so、libc.so。

实际导入包括AAssetManager/AAsset、pthread/TLS/condition/rwlock、clock_gettime、socket、EGL及ANativeWindow_fromSurface。静态依赖存在不等于战斗推进必调用渲染/网络；也不允许随意stub这些路径。

## P1 调查路线与时间盒

仓库外已有2026-09-27的独立运行时及resident supervisor：原字节libg + QEMU ARM Linux user-mode + Android Bionic/ART/JNI/AssetManager。它不启动完整Android Emulator，但仍依赖Android用户态库，不是宿主ISA原生执行，也不是零Android依赖。

历史记录声称固定工程seed的0/1/28动作完整终局和31,670次Android快照对照一致；这里只标历史证据，不能代替本次复验。2026-09-08 TRAINING_ENGINE_PLAN.md 关于x86上原生路线的判断已被后续方案超越。

P1从首次隔离启动起限时30分钟。先复用已存在的user-mode harness，在新进程中验证初始化、合法动作与执行回执、真实观测、逐段tick和自然终局。只在完整成功后给出NATIVE_HEADLESS_FEASIBLE；缺依赖或失败分别保留BLOCKED/INCONCLUSIVE证据，不伪造终局。当前P1为NOT_RUN，决策待本次执行。
