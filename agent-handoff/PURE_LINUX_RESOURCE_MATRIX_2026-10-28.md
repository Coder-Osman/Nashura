# E2.1-N：真实引擎资源路由矩阵

分支 `codex/pure-linux-libg-headless-20261026`，基线
`afdeb3333f5ec08fc2e6a71343b918207c9a878b`。在既有 Ubuntu ARM64 guest 的独立目录中，
用固定合成文件作为资源，通过真实 ARM64 `libg.so` 的 filesystem open 和原生文件
虚表运行矩阵。测试没有替换原始 APK、引擎库或已有资源；没有调用战斗对象图。

## 结果

第三轮 13 个独立进程均符合 E1 报告中对应的路由规则，矩阵驱动 exit 0，独立
SHA 绑定验收器 exit 0。文件存在的9例均 native/wrapper exit 0，原生 read/EOF/seek、
文件类型、字节 SHA、provider release 和 asset finish 均通过；缺失的4例 native
exit 60、wrapper exit 1，均明确记录缺失，且观察到 provider 槽释放、`dlclose` 和
asset finish。该观察只覆盖本 probe 调用的对象，不是完整引擎生命周期或全堆泄漏验证。

| 情形 | 无前缀 | `data:/` | `update:/` |
|---|---|---|---|
| 仅 APK | APK | APK | 缺失 |
| 仅 update | update | 缺失 | update |
| 同名不同内容 | update | APK | update |
| 两边均缺失 | 缺失 | 未测 | 未测 |
| 删除同名 update | APK | APK | 缺失 |

本矩阵使用合成文件且运行真实 libg，不是自然游戏资源覆盖。对照依据是既有 Android
E1 报告中的对应路由规则，并未用相同字节在 Android 和 Linux 做配对运行；非法路径、
损坏数据、descriptor、FMOD、自然音频及完整 catalog 均未覆盖。现有成功文件检查
覆盖完整读取、长度、EOF、回到起点和首字节重读，**不是 E1 的 24 操作全矩阵**。

## 首轮失败与修复

首轮为 12/13；`data:/` 查询仅存在于 update 的路径时，原生 open 返回非空 APK 文件
包装对象，但其 `valid` 返回 false。旧 probe 仍无条件调用 length，backend 对空 asset
句柄以 native exit 95 失败。根据 E1 真实诊断入口顺序，修正为先检查 valid，无效时
先关闭包装对象并报告缺失，不调用 length。第二轮同一案例返回 native exit 60，
记录了包装对象 close；整体 provider 清理当时仍为进程退出。第三轮将缺失作为诊断
返回码传出，在返回60前执行 provider 槽释放、`dlclose` 和 asset finish；13/13通过。
首轮、第二轮矩阵和原始日志保留。
没有改变原始 libg、资源 backend 或 Android 既有证据。

## 身份、命令及验收

- 原版 libg SHA：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- 既有资源 backend SHA：`905fb58cc33ed308107d433c6c1f9cfd57f6b6e121ff5e77dd1896176e11ea84`。
- 修复前 probe SHA：`f9f9307ab34d5dd6e3c6cfce8fb844dd7ff3cbdc4c8b9b255564153498919a74`。
- 第二轮 probe SHA：`6538c2acc470c9992a431b26a390d7035206c033f70f2a4f59252a8630278f90`。
- 第三轮 probe SHA：`b577eb66efb380b5e1a7d09b7ea15ae2a43158a515ae31cd8e7192c6a48d1d7b`。
- 首轮矩阵 SHA：`34157f9cdefb2a1361a127aceaa498c431f13dc293988a2029053a06997b37ce`。
- 第二轮矩阵 SHA：`ad66083f0f2b4080f21955a262fa612021b27254c232efcc954df84a249638ac`。
- 第三轮矩阵 SHA：`db7d512572c70ec9b547eccae86848fe97bb0833acf57bef3584cd0a3221a38d`。
- 第三轮独立验收结果 SHA：`5c2948df7203877fb4083fd6a8c1be06f19ccffad2ae0508a4e1ba471295c00a`。

在宿主使用已有 NDK27.3 ARM64 API30 编译真实 native probe，exit 0；随后在
guest `aarch64` / Linux `6.8.0-138-generic` 使用已有 API30 linker/Bionic：

```sh
"$NDK_CLANG" -Wall -Wextra -Werror -O0 -g -fPIE -pie -pthread \
  -Wl,--export-dynamic tools/pure_linux/native_init_probe.c \
  tools/pure_linux/provider_probe.c -ldl -o "$PRIVATE/probe-v3"
python3 tools/pure_linux/run_resource_matrix.py \
  --payload "$GUEST_PAYLOAD" --probe "$GUEST/probe-v3" \
  --backend "$GUEST_BACKEND" --output "$GUEST/results-v3"
# exit 0；首轮旧 probe 驱动 exit 1，第二轮 exit 0，均保留独立目录
python3 tools/pure_linux/verify_resource_matrix.py \
  --root "$GUEST/results-v3" \
  --matrix-sha256 db7d512572c70ec9b547eccae86848fe97bb0833acf57bef3584cd0a3221a38d \
  --probe-sha256 b577eb66efb380b5e1a7d09b7ea15ae2a43158a515ae31cd8e7192c6a48d1d7b \
  --backend-sha256 905fb58cc33ed308107d433c6c1f9cfd57f6b6e121ff5e77dd1896176e11ea84 \
  --output "$GUEST/results-v3/verification.json"
# exit 0；限定 PASS，catalog_ready_pass=false
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0；973 passed, 1 skipped
```

验收器重核13例全部原始 result/日志/有效资源输出 SHA，以及每例 libg、runtime、
probe 和 backend 身份；缺失例核实 provider/asset finish 和必要包装对象关闭，不推断
完整引擎资源全部回收。
输入与输出哈希及机器摘要见[证据](pure-linux-resource-matrix-evidence-20261028.json)。

## 门状态

本阶段 `ROUTING_MATRIX_PASS_LIMITED`，严格 `CATALOG_READY_PASS=false`。
Backup 服务等价、真实资源内容解析、Android 配对字节和异常域仍未闭合；
ResourceManager、GameState、Step、完整战斗、no-VM 验收与 10× 均 `NOT_RUN`。
主工程检查 `211 passed`，独立副本公开回归 `1,104 passed, 42 skipped`，均 exit 0；
这两组使用第二轮源码快照，第三轮的 Linux probe 清理修改由真实第三轮及 Linux
专项重新验证，不能把前两组写成第三轮源码回归。

下一步优先补齐相同内容的 Android/Linux 原生逐项比较及更广的资源异常覆盖，
并继续验证 Backup 所有权与 catalog 内容。通过严格门之前不进入战斗对象图。
