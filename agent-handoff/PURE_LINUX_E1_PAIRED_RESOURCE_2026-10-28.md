# E2.1-N：Android E1 与 Linux 真实引擎资源对照

基线 `5ea2b76`，工作分支 `codex/pure-linux-libg-headless-20261026`。历史
Android E1 私有归档与本次 Ubuntu ARM64 guest 中的真实 `libg.so` 独立进程
进行 24 项资源路由及文件操作对照。

## 结果及范围

**PASS_LIMITED**：24/24 案例的字节、原生返回状态及每例24项文件操作一致；
包含9项 APK、6项 update、9项缺失。驱动和独立校验均退出0。缺失案例验证了
本 probe 的包装对象关闭、provider 槽释放和 asset finish；不代表全进程无泄漏。

Android 结果来自先前 E1 归档，不是同时运行的配对实验。Linux 使用归档中的字节
建立两个 APK 文件和合成 update 文件；APK 文件字节与用户提供的 APK 一致。
Linux 在已打开的文件句柄上重置并执行文件操作；Android E1 使用第二个新打开
的句柄。此次比较未覆盖自然 FMOD、完整 catalog、Backup、全部资源或完整战斗。
保持 `CATALOG_READY_PASS=false`，ResourceManager、GameState 和 Step 未运行。

## 身份

- Android 归档 SHA256：`5548776e26db3c4a79235358f0e097d92cd5ea6fac7cfb78ab4a6ab2e05830d5`。
- 用户 APK SHA256：`a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f`；不替代历史 APK 锁定身份。
- 两个 APK 资源 SHA256：`a0ad74eff207e83672c7b12eb5515199c08a4f989b7eebc5f8a8c642421f4066`、`83209d292fd62355bf165470db4e1fcd9d3e78ae16ca8768f6fcba5e16f6fea5`。
- libg SHA256：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- Linux probe SHA256：`d7f403903a86e7b814306ef7c7802f5faf71ba5e17a769bb94c0bb3e183d92e1`。
- backend SHA256：`905fb58cc33ed308107d433c6c1f9cfd57f6b6e121ff5e77dd1896176e11ea84`。
- 矩阵 SHA256：`53bdc82c21386807d8d3fded2db8a8fc673b611e9bcfed198e96b1b13a7a8de4`。
- 独立校验结果 SHA256：`0ec672280f0f40d562985bdce9587861c4ab5fd488decb4ecd092995909a0c80`。

## 实际验证

现有 Ubuntu aarch64 guest，使用现有 Android linker/Bionic 与真实引擎；Linux 24 个
进程各自保留结果、资源及日志。私有根 `/tmp/nashura-e1-paired-20261028-v1`。
归档、资源、游戏库和原始日志不公开。

```sh
python3 tools/pure_linux/run_e1_paired_resource_matrix.py \
  --stock "$STOCK" --payload "$PAYLOAD" --probe "$PROBE" \
  --backend "$BACKEND" --output "$RESULTS"
# exit 0, 24/24
python3 verify_e1_paired_resource_matrix.py --root "$RESULTS" --stock "$STOCK" \
  --matrix-sha256 53bdc82c21386807d8d3fded2db8a8fc673b611e9bcfed198e96b1b13a7a8de4 \
  --probe-sha256 d7f403903a86e7b814306ef7c7802f5faf71ba5e17a769bb94c0bb3e183d92e1 \
  --backend-sha256 905fb58cc33ed308107d433c6c1f9cfd57f6b6e121ff5e77dd1896176e11ea84 \
  --output "$RESULTS/verification.json"
# exit 0
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# 981 passed, 1 skipped; exit 0
```

专项包含合成 fixture 的篡改拒收测试；这些测试不替代真实运行证据。已授权的
真实配置单事务写入和恢复此前完成，并在配置身份报告中保留证据；本次未重复。

下一步仍是闭合 Android Backup 服务副作用、配置异常域及自然 catalog 的严格
对照。不得以本资源子项通过推断 no-VM、Android-free 或性能通过。

[机器证据](pure-linux-e1-paired-resource-evidence-20261028.json)；
[回执](CODEX_REPLY_2026-10-28_E1_PAIRED_RESOURCE.md)。
