# 本机（Apple Silicon macOS）采集 + 训练部署

本文记录在 **Apple Silicon macOS（M4 Max）** 上跑通事件观测采集与事件策略预训练的全部步骤、
踩到的坑，以及实测效率。参考实现是仓库里的 `tools/bootstrap_capture_node.sh`（面向
x86_64 Linux + KVM 采集节点），本机与它的差异只有两点：

1. 本机用 **arm64-v8a 系统镜像**（Hypervisor.framework 原生虚拟化，不需要 `/dev/kvm`），
   引擎以原生 AArch64 运行，而不是 x86_64 上的 libndk_translation 翻译执行；
2. 正因如此，probe 需要一处 **ARM64 TBI 指针 tag 兼容修复**（见第 8 节），否则
   `observe` 会以 `native object identity failed validation` 失败。

---

## 1. 前置条件

| 组件 | 版本 / 路径 |
| --- | --- |
| Android SDK | `~/Library/Android/sdk`（platform-tools、emulator、cmdline-tools） |
| 系统镜像 | `system-images;android-30;google_apis;arm64-v8a` |
| AVD | 名称 `astral-api30`，`hw.cpu.ncore=6`（必须与引擎数严格相等） |
| NDK | `~/Library/Android/sdk/ndk/27.3.13750724`（r27c 亦可） |
| 离线 APK | `/tmp/cr-ai-offline6.apk`（含 `libg.so` + probe，签名后 795 MB） |
| 原始 APK | `~/Downloads/nr_15.535.13_release_ff1c6c29.apk`（内容来源，见 §3） |
| Python | 3.13 + torch 2.9（`mps` 可用）、numpy/orjson/safetensors/zstandard/pyarrow |

`adb` 必须在 `PATH` 里，`configure_emulator_engine_guests.py` 直接调用 `adb`：

```bash
export PATH="$PATH:$HOME/Library/Android/sdk/platform-tools"
```

---

## 2. 游戏内容必须完整（否则 observe 全挂）

离线 APK 只带引擎，**不带 `assets/firstlight/` 资源**；设备上的
`/data/data/nullsroyale.rel.free/update/` 必须由「先手动打开原版游戏完成资源更新」产生。
本机 AVD 曾长期报：

```
Device update is incomplete or unsupported: runtime-update/csv_logic/characters/ghost_ev1.toml
```

原因不是内容版本不对（`fingerprint.json`、`libg.so` 哈希都匹配），而是 update 目录里
**恰好缺 4 个 toml**。这 4 个文件的正确字节就在原始 APK 的 `assets/csv_logic/` 下，
且哈希与 `native_runner/data/competitive/manifest.json` 的 `source_files` 期望值逐字节相同，
可以直接补齐（无需联网、无需重开游戏）：

```bash
APK=~/Downloads/nr_15.535.13_release_ff1c6c29.apk
D=/data/data/nullsroyale.rel.free/update/csv_logic/characters
cd /tmp && rm -rf apkprobe && mkdir apkprobe && cd apkprobe
for f in characters/ghost_ev1.toml characters/goblin_hut_rework.toml \
         characters/hero_form/balloon_hero_spell.toml characters/skeleton_balloon_ev1.toml; do
  unzip -o -q "$APK" "assets/csv_logic/$f" -d .
done
adb root
adb push ghost_ev1.toml              $D/ghost_ev1.toml
adb push goblin_hut_rework.toml      $D/goblin_hut_rework.toml
adb push skeleton_balloon_ev1.toml   $D/skeleton_balloon_ev1.toml
adb push hero_form/balloon_hero_spell.toml $D/hero_form/balloon_hero_spell.toml
adb shell "chown 10168:10168 $D/ghost_ev1.toml $D/goblin_hut_rework.toml \
  $D/skeleton_balloon_ev1.toml $D/hero_form/balloon_hero_spell.toml; \
  chmod 600 $D/ghost_ev1.toml $D/goblin_hut_rework.toml \
  $D/skeleton_balloon_ev1.toml $D/hero_form/balloon_hero_spell.toml"
```

校验（期望输出 `files: 990` + `VALIDATION OK`）：

```bash
cd ~/Documents/ChatGPT/CR/firstlight_CR
python3 -c "
from native_runner.offline_install import Device, resource_hashes, validate_resources
d = Device('$HOME/Library/Android/sdk/platform-tools/adb', 'emulator-5554')
h = resource_hashes(d); print('files:', len(h)); validate_resources(h); print('VALIDATION OK')"
```

> 注意：`chown` 的 uid 是应用 uid（本例 `10168`），不是 0；否则应用读不到文件。

---

## 3. 编译 probe

用与 `tools/bootstrap_capture_node.sh` 相同的编译参数，只把工具链换成 `darwin-x86_64`：

```bash
CXX=~/Library/Android/sdk/ndk/27.3.13750724/toolchains/llvm/prebuilt/darwin-x86_64/bin/aarch64-linux-android24-clang++
cd ~/Documents/ChatGPT/CR/firstlight_CR/native_runner/probe
mkdir -p out
$CXX -std=c++17 -O2 -fPIC -fvisibility=hidden -Wall -Wextra -shared \
  -Wl,-z,max-page-size=16384 -o out/libcrprobe.so \
  cr_replay_probe.cpp native_touch_interceptor.cpp \
  native_call_arm64.S remaining_runtime_arm64.S -llog -ldl -lz
shasum -a 256 out/libcrprobe.so
```

热替换到已安装的 APK（必须先 `force-stop`，否则进程还映射着旧文件）：

```bash
adb shell am force-stop --user 0 nullsroyale.rel.free
T=$(adb shell pm path nullsroyale.rel.free | tr -d '\r' | sed 's#package:##')
D=$(dirname "$T")/lib/arm64
adb push native_runner/probe/out/libcrprobe.so /data/local/tmp/crprobe_new.so
adb shell "dd if=/data/local/tmp/crprobe_new.so of=$D/libcrprobe.so conv=fsync; sha256sum $D/libcrprobe.so"
```

---

## 4. 启动并配置 6 个引擎

6 个引擎是同一个包的 6 个独立进程（`nullsroyale.rel.free:engine0..5`），
分别监听 `26789..26794`：

```bash
for i in 0 1 2 3 4 5; do
  adb shell "am start --user 0 -f 0x18000000 \
    -n nullsroyale.rel.free/com.supercell.clashroyale.Engine${i}App"
  sleep 1
done
sleep 40
cd ~/Documents/ChatGPT/CR/firstlight_CR
python3 native_runner/training/v4/configure_emulator_engine_guests.py \
  --serials emulator-5554 --host-bases 26789 --engines 6
```

成功后返回 `"ok": true`，各引擎为 `coldReady`。
`configure_emulator_engine_guests.py` 要求 guest 的 `nproc` **严格等于** `--engines`，
所以 AVD 的 `hw.cpu.ncore` 必须改成 6（默认 8 会直接报
`emulator-5554 exposes 8 CPUs, expected 6`）。

应用重启后引擎会报 `native content runtime is not initialized yet`，重跑一次上面的
configure 即可。

---

## 5. 数据集

HF 上的 `VanguardX101/IL_Replay` 本地直连被墙，用 hf-mirror：

```bash
mkdir -p ~/cr_local/datasets/IL_Replay/{replays,actions}
B=https://hf-mirror.com/datasets/VanguardX101/IL_Replay/resolve/main
curl -fL --retry 5 -o ~/cr_local/datasets/IL_Replay/replays/part-000000.parquet "$B/replays/part-000000.parquet"
curl -fL --retry 5 -o ~/cr_local/datasets/IL_Replay/actions/part-000000.parquet  "$B/actions/part-000000.parquet"
```

`part-000000` 一个分片 = **5000 场**（`pyarrow.parquet.read_table(...).num_rows`）。
完整 HF 数据集共 52 个分片，`SOURCE_COUNT=252238`。

---

## 6. 采集

```bash
cd ~/Documents/ChatGPT/CR/firstlight_CR
python3 tools/build_event_observations.py \
  ~/cr_local/datasets/IL_Replay ~/cr_local/cache-240 \
  --ports 26789,26790,26791,26792,26793,26794 \
  --source-count 5000 --replays-per-shard 1 --max-shards 240
```

- `--source-count` **必须**传该 parquet 的真实场数（0 会被判非法）。
- 输出 `.crev.zst` 分片 + `index.json` + `failures.json`。
- 流式训练请加 `--watch-seconds / --min-new-shards / --min-age-seconds`，
  或用 `tools/push_event_cache.sh` + `tools/promote_event_cache.sh` 推给训练机。

### 实测（6 引擎，235/240 场）

| 指标 | 数值 |
| --- | --- |
| 完成场次 | 235（失败 5，全部是「找不到与公开出牌序列相容的发牌」这一类） |
| 墙钟 | 279.4 s |
| 吞吐 | **0.84 场/s ≈ 3,030 场/小时**（6 引擎） |
| 单引擎 | ≈ 505 场/小时 |
| 帧 | 37,430（其中 ACT 27,554） |
| `legal_act_fraction` | 0.388 |
| 产物体积 | 45 MB / 235 场 ≈ **195 KB/场** |

按 195 KB/场推算，HF 全量 252,238 场的事件观测缓存约 **49 GB**；
本机 6 引擎全量约 **83 小时（≈3.5 天）**。

参考：x86_64 采集节点 24 引擎实测 2000 场/1615 s ≈ 1.24 场/s，即单引擎 0.052 场/s；
本机原生 AArch64 单引擎 ≈ 0.14 场/s，**约为翻译执行节点的 2.7 倍**。

---

## 7. 训练

```bash
cd ~/Documents/ChatGPT/CR/firstlight_CR
python3 tools/train_event_pretrain.py \
  --cache-root ~/cr_local/cache-240 --output-dir ~/cr_local/run-240 \
  --epochs 1 --batch-shards 8 --device mps \
  --val-fraction 0.2 --val-samples 16 \
  --delay-coarse-weight 0.3 --delay-coarse-group 5 --delay-lr-mult 2
```

单卡本机直接用 `--device cpu`；多卡/多机仍用 `torchrun --nproc_per_node=N`。
验证集切分是按分片名做确定性哈希（`sha1(f"{seed}:{name}") % 10000`），多 epoch 时有
`--early-stop-patience` 与 `--adaptive-epochs/--gap-tolerance` 兜住过拟合；
不要为了让 loss 好看而关掉它们。

> **本机务必用 `--device cpu`**：同样的 6 个 batch（`--batch-shards 2`）实测
> MPS `real 57.9s`、CPU `real 24.2s`，即 MPS 比 CPU **慢约 2.4 倍**。
> 事件模型算子碎、单批行数小，MPS 的逐 op 调度/同步开销盖过了 GPU 收益。

---

## 8. 采集正确性验证（已做）

数据落盘后做了两项独立验证，均通过。

**(a) 引擎侧 ring 一致性**（`<cache>/stats.json`，235 条）：

| 指标 | 结果 |
| --- | --- |
| `completed` | 235 / 235 |
| `ring_mismatches` | **全 0**（native 部署 ring 与调度时间线逐场完全一致） |

**(b) 标签语义**（重新解压 235 个分片、逐条读 `EventObservationRecordV1`）：

| 指标 | 本机实测 | 服务端修正后基准 |
| --- | --- | --- |
| 帧数 | 37,430 | — |
| ACT / WAIT | 27,554 / 9,876（2.79:1） | 2.44:1 |
| `legal_act_fraction` | **0.388** | ~0.38 |
| **delay-0 占比** | **0.0133** | ~0.013 |
| ACT `card_in_hand` | 0.410 | — |
| 手牌槽位分布（slot 0/1/2/3） | 6808 / 6972 / 6946 / 6828 | 近似均匀 |
| 延迟 top bins | 14–24（1.4–2.4 s） | 非 0 坍塌 |
| 网格越界 / 非 100ms 延迟 / 时序倒挂 | **0** | 0 |

其中 **delay-0 占比 1.3%** 是关键回归项：旧的 HF 标签用 `>=` 判据会把 57.5% 的标签
塌到 delay-0（自预测），本机采集已确定是严格 `>` 语义。分片 `verify=True` 的
`compressed_sha256` 校验也全部通过。

同时抽查到 `observation_tick = 217` 对应记录中 tick 216 的出牌，确认
**观测 tick = play_tick + 1**（实体与 ring 都在下一 tick 才可见）这条不变量在本机成立。

---

## 9. 关键修复：ARM64 scudo 指针 tag 与 `/proc/self/maps`


**症状**：本机所有引擎的 `observe` 都以
`RunnerError: invalid request to 'observe'` / `invalid native runner response` 失败，
响应体被截断在 `"objects":[`。

**定位**（已在 probe 中保留 fail-closed 诊断输出）：

```json
{"ok":false,"error":"native object identity failed validation",
 "requestedSlot":0,"requestedReadable":false,"count":6,
 "slots":[{"slot":0,"address":"0xb4000076d460ab70","readable":false,...}],
 "mapsOpen":true,"mapsLines":2859,"mapsRangeFound":false}
```

对象指针全是 `0xb4000076…`，但 `/proc/self/maps` 里**没有任何 `b4…` 开头的映射**，
真实映射是 `76d4526000-76d4626000 rw-p [anon:scudo:primary]`。

**根因**：原生 AArch64 上 Bionic 的 scudo 会给堆指针打 **TBI 高字节 tag**
（`0xb4`）。CPU 读写时硬件忽略高字节，所以普通字段读取都正常；但
probe 的 `process_range_is_readable()` 拿**带 tag 的数值**去和 `/proc/self/maps`
比较，必然 miss，于是所有堆对象都被判为不可读，`observe` fail-closed。
x86_64 采集节点上指针不带 tag，所以从未暴露。

**修复**（`native_runner/probe/cr_replay_probe.cpp`）：

```cpp
constexpr std::uintptr_t kAddressTagMask = 0x00FFFFFFFFFFFFFFULL;
std::uintptr_t untagged_address(const void *address) {
  return reinterpret_cast<std::uintptr_t>(address) & kAddressTagMask;
}
```

- `process_range_is_readable()`：`begin` 先过 `untagged_address()`（`end` 由 `begin+size` 得出）；
- `make_page_writable()`：同样先去掉 tag，再算页地址调 `mprotect`。

对翻译执行的 x86_64 节点是 no-op（高字节本来就是 0），因此该修复对现有采集节点无副作用。

---

## 10. 已知限制 / 待办

- `build_event_observations.py` 目前用 **Python checkpoint 逐步**推进（`--step-until-event`
  未在本机验证），比服务端默认路径慢；若要提速可再验证 native 事件模式（仅支持单 lane/引擎）。
- 本机训练请用 `--device cpu`（MPS 慢 2.4 倍）；但即使 CPU，单 epoch 也远慢于 8×V100，
  大规模预训练仍应放到 GPU 训练机。
- AVD 只配了 6 核 = 6 引擎。要再多开引擎需要同时提高 `hw.cpu.ncore`（configure 要求严格相等）。
- 采集失败率约 2%，全部是发牌校准不兼容（`找不到与公开出牌序列相容的发牌`），
  与内容/引擎无关，重跑或换场次即可。

---

## 14. 已否决的优化：`--step-until-event` 与原路径**不等价**

原生 `--step-until-event` 在测试 AVD 上实测确实更快（同一批 12 个分片、2 个引擎：

| | Python 落点法（现用） | `--step-until-event` |
| --- | --- | --- |
| 工具自报 elapsed | 21.4 s | **13.9 s**（1.54×） |
| frames / act / wait / events / plays / legal | 1342 / 840 / 502 / 668 / 687 / 302 | 完全相同 |

但**逐字节与逐张量比对后不通过**：12/12 分片的容器不同，且差异不只是压缩产物——
`records`（标签：card/cell/delay/legality）完全一致，**观测张量有 20/89 组不同**，
其中 `observations.events.event_type` 有 577/2176 个元素不同（maxabs 38），
`events.source_card_vocab_id` 差到 112，`events.mask` 有 124/2176 不同，
`groups` / `match_scalars` 也有约 0.1 的浮点差异。

也就是说两条路径对「决策刻可见的 recent-events 窗口」的判断不同，标签虽然一样，
**模型训练所依赖的观测分布不一样**。把这条路径混进同一份缓存会静默污染训练数据，
因此不采用；现有的 13k+ 分片全部来自落点法，且其标签分布已对齐服务端修正后的基准
（delay-0 1.3%、ACT:WAIT 2.79、legal_act_fraction 0.388）。

> 教训：`EVENT_DRIVEN_SPEC.md` 里「34 个停止刻全部等于出牌刻+1、0 缺失 0 多余」只验证了
> **停止刻**，没有验证完整语义观测。要做路径替换，必须做逐张量等价性验证。

## 15. 吞吐瓶颈与可扩展性（实测）

- 本机 14 核（10P+4E），采集只用到约 **4.5 核**（6 个 worker 各 50–90% + qemu ~1.4 核），
  父进程几乎空转，仍有约 9 核空闲。
- 压缩不是瓶颈：默认已是 zstd 最快档 `--compression-level -5`，压缩比约 **50.8×**
  （container 中位 7.44 MB → 压缩后 0.16 MB）。
- 瓶颈是**每场 replay 的原生往返次数 + Python 张量化**：单引擎约 2.9 s/场
  （6 引擎合计 1.8–2.1 场/s）。worker 未跑满说明还有并发空间。

因此**增加引擎/AVD 可以提升吞吐**，但前提是有「未被占用的分片」：改动 `partition_count`
会让已有分片的 id 与场次错位（id 是分区内计数器），所以**不能中途重新分区**。
在这台机器上唯一能立刻叠加的工作是 node B 的那一半（`PARTITION_INDEX=1`），
而那会与另一台机器的计划冲突，属于需要人工决策的事。

---

## 16. 中途可复核：`progress.json` 与「可复现」验证

`index.json` / `stats.json` 只在整轮采集结束时写，长跑中途没有任何落盘的
`ring_mismatches`。已改为**每轮（默认 300 片）增量刷新 `<cache>/progress.json`**，
累计字段含 `shards / empty_shards / frames / act_frames / legal_act_frames /
ring_mismatches / rounds`，并打印 `event-round` 日志行。实测
`shards(13999) + empty_shards(401) == selected_replays(14400) == 48 轮 × 300`。

顺带验证了**可复现性**（这是比字节复现更实在的性质）：在另一个 AVD（独立模拟器、
不同进程、数小时后）重采同样的分片，与正式缓存逐条比对——

| | 结果 |
| --- | --- |
| `records`（标签：card/cell/delay/legality） | **完全相同** |
| 观测张量 payload | **逐字节相同**（4027140 / 2240772 字节） |
| manifest 差异 | 仅 `contract.selected_replays`（2500 vs 6，信息字段） |

即同一场 replay 在不同引擎实例上产生完全一致的观测与标签。
另外把 `cache-nodeA/` 里早期 2 片测试残留的 `index.json` / `stats.json`
改名为 `*.stale-2shard-test`，避免中途被误读为「分片数只有 2」。
