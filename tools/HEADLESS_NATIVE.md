# 原版 ARM64 headless 工程入口

此入口只创建自己的QEMU Linux user-mode子进程，不调用既有服务管理器，不触碰训练配置。依赖用户私下准备的匹配运行时；仓库不提供APK、原库、提取资源、DEX或引擎二进制。

## 定义与边界

- 脱离的是完整Android Emulator/虚拟设备；仍使用Android Bionic、ART/JNI、AssetManager和原版逻辑资源。
- 本工具不是去Android化、宿主ISA直接执行或自有规则引擎。
- 不读取模型、不训练、不改变FAIR/Replay和默认训练后端。完整原生观测只作为私有工程审计，禁止输入策略。
- 输出目录必须全新且在冻结输入外，使用动态loopback端口，核对socket属于本工具的子进程后才发送命令。只回收自身Popen对象。

## 固定输入

`--runtime`指向私有standalone-engine运行时，`--resident`指向对应resident构建。必须显式提供probe/bootstrap/dex/verifier的SHA；原库SHA固定为本项目支持版本。启动前调用冻结来源校验器核验运行时闭包。

工程fixture采用独立schema，不得包装为训练数据：

```json
{"schema":"headless-native-fixture.v1","match":{"cmd":[],"evt":[]},"actions":[{"tick":90,"command":{"ct":86,"c":{}}}]}
```

示例只说明结构；match/c必须是用户已有且核验的完整原生命令，不能把此空对象当作实际成功样本。命令执行前核对当前手牌、打包卡身份及圣水；终局后核对每条原生card_play消费回执及t2。

## 执行

```sh
python tools/headless_native.py \
  --runtime "$PRIVATE_RUNTIME" --resident "$PRIVATE_RESIDENT" \
  --output "$NEW_EVIDENCE_DIR" --fixture "$ENGINEERING_FIXTURE" \
  --probe-sha256 "$PROBE_SHA256" --bootstrap-sha256 "$BOOTSTRAP_SHA256" \
  --dex-sha256 "$DEX_SHA256" --verifier-sha256 "$VERIFIER_SHA256" \
  --concurrency 1,2,4,8 --repeats 3 --observation-ticks 250
python -m pytest -q -p no:cacheprovider tools/tests/test_headless_native.py tools/tests/test_headless_trace_verifier.py
python tools/verify_headless_traces.py "$TRACE_LEFT" "$TRACE_RIGHT" --output "$NEW_VERIFICATION"
```

默认20tick观测用于细粒度检查；250tick是独立吞吐配置，不相互声称速度倍率。相同配置、动作流、观测周期才允许比较。基准计时包含reset和完整推进/观测，不含一次性启动；启动单列。CPU是子进程CPU秒/墙钟秒的核数等效，内存为每进程VmHWM；不是整机总体GPU/内存指标。

## 证据语义

原始逐边界capture压缩保存在私有输出中；公开results只放耗时、摘要和计数。generation/stateEpoch是进程/局身份；combat/remaining/phase流的sequence及causeSequence按各自epoch起点平移，deploymentSequence按首个部署ID平移。保留全部相对间隔、顺序、因果引用和战斗字段，不删除不认识的字段。

重置后原始遥测序号不归零是原接口语义；使用原始完整JSON直接算SHA会误报。独立归档比较器对所有边界进行精确字典比较，状态、事件、HP等任何差异均拒收。

目前原生可行性证据只覆盖固定工程卡组、seed和26动作轨迹，不是全部卡机制等价，也不是新FAIR训练准入。没有本次Android Emulator同轨迹新对照，不引用历史31,670快照作为本次结果。
