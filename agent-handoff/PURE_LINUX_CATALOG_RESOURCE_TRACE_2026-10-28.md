# E2.1-N：真实 loader 的 APK/update 资源观察

分支 `codex/pure-linux-libg-headless-20261026`，基线
`39a32fe46bede40004137e86a6d845f0d9979ff7`。本阶段重分析上一阶段真实 ARM64
Linux `libg.so` loader 的私有日志，没有重新运行引擎或修改资源；原始日志、资源和
二进制保持私有，历史结果原样保留。

## 观察结果

原进程 native exit 0，严格 wrapper exit 1。绑定 result、完整 strace 与 asset backend
stderr 的摘要如下：

| 观测项 | 数量 |
|---|---:|
| APK backend open/read/close | 各 247 次 |
| APK 索引不含的名称 | 261 次 |
| update 文件实际打开、完整映射、关闭 | 各 131 次 |
| update 打开前曾出现同路径成功 stat | 131 次 |
| APK 暂存及 update 同名文件 | 124 个 |
| 本次打开的 update 文件中，APK 也有同名副本 | 122 次 |
| 上述同名副本的清单 SHA 不同 | 118 次 |
| 本次同一路径同时打开 APK 和 update | 0 个 |

已观测文件描述符均关闭，strace 记录正常终止，解析器没有发现未完成调用或
无法解析的相对路径。读取/打开日志与 247 次 APK 路径系统调用计数一致。
这支持在当前自然运行及当前资源集合下发生了 update 路径选择；记录按路径集合
对照，没有将跨日志的顺序认定为单次请求因果，也没有断言所有资源、所有阶段均
遵循该选择。成功 stat 在对应 open 之前出现不证明两者处于同一个引擎调用。

`mmap` 只证明文件区间被映射，**不证明 CPU 消费全部映射字节、解析内容正确或
Android 逐项一致**。清单中的 SHA 是实验前后文件身份，不是读取缓冲区 hash。
APK 未命中次数也不等于回退成功次数。本结果不代替 Android 已有 E1 矩阵，也不
直接证明 Linux 对冲突、缺失和非法路径的完整语义。

## 实现与命令

新增 `tools/pure_linux/catalog_resource_audit.py`，使用现有 `resource_trace.analyze`
重建 fd、stat、mmap、close，并绑定原 result 中的各日志 SHA、资源清单和输入前后
身份；要求 trace 完整退出、路径均可解析、已记录资源在清单中、backend open/close
与实际文件 open 数量一致。输出仅包含总量、SHA 和证据边界，不输出资源名称、原始
syscall 或私有内容。调用方提供的根路径本身需要按实验上下文核验；工具不把它们
提升为 Android 等价性证明。

```sh
python3 tools/pure_linux/catalog_resource_audit.py \
  --result "$PRIVATE/result.json" \
  --result-sha256 caad1b680c3a52efb00d49ed8e7762d77e474f5600df788e8ac684006d093014 \
  --trace "$PRIVATE/syscalls.14721" --stderr "$PRIVATE/stderr.log" \
  --assets-root "$GUEST_ASSETS" --update-root "$GUEST_UPDATE" \
  --cwd "$GUEST_EXPERIMENT/candidate" --output "$PRIVATE/catalog-resource-audit-v2.json"
# exit 0；输出独占创建

/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0；964 passed, 1 skipped
```

`tools/run_main_engine_checks.py` exit 0（211 passed），源码清单 2,591 项通过，
gitleaks exit 0、无发现。最新一次公开回归在父提交完成；本阶段仅新增独立审计器，
没有重复完整公开回归。`git fetch origin --prune` 因 GitHub 443 连接超时退出 128，
该次没有取得新的远端状态；本地基线和现有远端跟踪分支均为上述 `39a32fe`。

固定输入的 result SHA `caad1b680c3a52efb00d49ed8e7762d77e474f5600df788e8ac684006d093014`，
syscall SHA `0a34bb830c6e805de966479b0cb8276b7a8b6068d76cd440b32c8c6b758ca071`，
backend stderr SHA `d17169692bc04fada60abc7fe686c1ac4abbdcd58589e89ceaa7edf22d45b875`，
摘要 SHA `8c9beee1ec4885338625f80e35ca65e50902408aa7c3ebd9540a14302469b7d7`。
tool SHA `c7474e3d8c33c05e1821fbaa0ec437ac9a6ad5b7984ee88b70c5b0136a7b4a8c`。
独立测试覆盖 SHA 篡改、缺失关闭、trace 未完成、backend 失败、缺失 stat 及根路径
重叠；合成测试不代替真实引擎证据。

## 门状态

本阶段为 **RUNTIME_OBSERVED_LIMITED**。`catalog_ready_pass=false`；Backup 服务
等价、逐请求 namespace/fallback 因果、资源字节和 catalog 内容对照仍未闭合。
ResourceManager、GameState、真实 Step、完整局、无 JavaVM 验收和 10× 均 `NOT_RUN`。
下一步应在独立输入副本中构造 APK/update 冲突和缺失矩阵，通过原生引擎入口读取并
与已核验 Android E1 同类行为比较；严格 catalog 门通过前不推进战斗对象图。

机器摘要：[JSON](pure-linux-catalog-resource-trace-evidence-20261028.json)。
