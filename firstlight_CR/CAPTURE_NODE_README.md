# 采集节点一键部署（Capture node bootstrap）

新租的采集服务器到手后按下面三步走。**采集不需要 GPU**，但必须能用 `/dev/kvm`
（裸金属，或开启了嵌套虚拟化的 VM）。

## 0. 选机器（实测比例）

| 资源 | 实测消耗 |
|---|---|
| 每个 AVD（含 6 引擎） | ~14GB 内存、~5GB 磁盘 |
| 每个引擎 | 约 2–3 核、~3–4GB 内存 |
| 64 核 / 256GB 跑 24 引擎 | 内存 ~94GB，产出 ~4,500 局/小时 |

经验配置：**64 核 / 256GB / 500GB+ NVMe / x86_64 / Ubuntu 24.04 / KVM 可用**（无 GPU）。
32 核 / 128GB 可跑 12 引擎。别把引擎塞太满：探针有已知原生崩溃，密度低一点崩溃波及小。

## 1. 准备

```bash
sudo usermod -aG kvm $USER        # 之后重新登录，确认 `id | grep kvm`
tar xzf firstlight_capture_bundle.tgz
cd firstlight_CR
python3.12 -m venv .venv          # 或 python3 -m venv --without-pip + get-pip
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu   # 纯采集 CPU 版即可
.venv/bin/pip install numpy orjson safetensors zstandard pyarrow
cp tools/capture_node.env.example capture_node.env
$EDITOR capture_node.env          # 至少改 AVD_COUNT / HOST_BASE / PARTITION_*
```

APK 放在 `../render-apk/cr-ai-offline6.apk`（随包提供），或用 `APK=` 指定路径。

## 2. 一键部署

```bash
bash tools/bootstrap_capture_node.sh capture_node.env all
```

它会依次：装 SDK/NDK/系统镜像 → 编译探针 `.so` → 建 AVD → 非流式装 APK 并热替换探针 →
起模拟器 → 配置引擎 → 下载 HF 数据集（`VanguardX101/IL_Replay`，52 分片）→ 起采集 + 看门狗。

也可以单步跑（可重复执行、幂等）：

```bash
bash tools/bootstrap_capture_node.sh capture_node.env install-deps
bash tools/bootstrap_capture_node.sh capture_node.env build-probe
bash tools/bootstrap_capture_node.sh capture_node.env prepare-avds
bash tools/bootstrap_capture_node.sh capture_node.env install-app
bash tools/bootstrap_capture_node.sh capture_node.env configure
bash tools/bootstrap_capture_node.sh capture_node.env dataset
bash tools/bootstrap_capture_node.sh capture_node.env run
```

## 3. 重要提醒

- **AVD 镜像建议迁移而不是新建**。APK 的 `.build.json` 写的是 `existing-device-update`，
  即依赖设备上已有的游戏资源更新；新建的 AVD 可能过不了资源校验/内容就绪。
  等旧机能连上时，在旧机上执行：
  ```bash
  tar czf avd.tgz -C ~/.android avd      # ~20GB
  ```
  把 `avd.tgz` 传到新机，在 `capture_node.env` 里设 `AVD_IMAGE_TARBALL=/path/avd.tgz`，
  再跑 `prepare-avds`（会自动解压并跳过新建）。
- **装 APK 必须 `--no-streaming`**，否则 `/data/app/<pkg>/lib/arm64` 是只读的 incremental-fs，
  探针 `.so` 换不进去（脚本已处理）。
- 多台机器分片：每台设 `PARTITION_INDEX=0..N-1`、`PARTITION_COUNT=N`，产出的分片不重名。
- 推给训练机：设 `TRAINER_TARGET=user@host:/path/event-staging` 和 `TRAINER_KEY=<私钥>`，
  会自动起 `push_event_cache.sh`。
- 观测：`tail -f $OUTPUT_DIR.log` 看采集；`/tmp/capture-watchdog.log` 看看门狗动作。
