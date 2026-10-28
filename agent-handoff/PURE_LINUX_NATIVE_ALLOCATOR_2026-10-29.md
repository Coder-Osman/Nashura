# E2.1-N：Linux 真正的引擎分配器

基线 `4b976e2`。本阶段为 `NATIVE_VERIFIED_LIMITED`；严格
`CATALOG_READY_PASS=false`。

在 Ubuntu ARM64 guest 的新隔离子进程中，加载匹配的真实 `libg.so`，使用
SHA、入口 16 字节及库内 `AndroidMainAllocator` 字符串校验后，调用引擎自身
构造函数 `0x1237348`，验证构造对象的虚表、第二字段和前后 canary。通过真实
虚表分配 64 字节、验证对齐及完整写入、调用真实释放函数；非 2 的幂对齐 12 和
超过 32 位大小分别返回空指针。对象 canary 未改变。该构造对象位于实验子进程
的本地有界存储，与 Android 上嵌入父对象的完整生命周期并不等同。

两次运行原生子进程 exit 0，输入、资源、probe 和辅助文件身份前后相同。第二次
增加两个拒绝边界，外层因严格配置与 Backup 契约仍为 `BLOCKED`；没有写入 ready，
没有调用未知工厂或 GameState。实验使用 strace，不是无插桩行为或 Android
轨迹等价证明；跨线程分配、释放后行为及完整分配器生命周期仍未验证。

## 验证

```sh
"$NDK_CLANG" -Wall -Wextra -Werror -O0 -g -fPIE -pie -pthread -Wl,--export-dynamic \
  tools/pure_linux/native_init_probe.c tools/pure_linux/provider_probe.c \
  tools/pure_linux/preference_store_hook.c tools/pure_linux/preference_store.c \
  tools/pure_linux/preference_files.c tools/pure_linux/preference_contract.c \
  tools/pure_linux/preference_codec.c tools/pure_linux/preference_cipher.c \
  tools/pure_linux/native_backup_queue.c -ldl -o "$PROBE"
# exit 0
python provider_job.py --payload "$PAYLOAD" --probe "$PROBE" --output "$OUT" \
  --mode both --data-root "$DATA" --update-root "$UPDATE" --data-path / \
  --search-root "$UPDATE" --resource-manifest "$MANIFEST" --asset-root "$DATA" \
  --asset-index "$INDEX" --asset-index-sha256 "$INDEX_SHA" \
  --asset-backend "$BACKEND" --asset-backend-sha256 "$BACKEND_SHA" \
  --preference-material "$MATERIAL" --preference-material-sha256 "$MATERIAL_SHA" \
  --preference-xml0 "$XML0" --preference-xml0-sha256 "$XML0_SHA" \
  --preference-xml1 "$XML1" --preference-xml1-sha256 "$XML1_SHA" \
  --native-backup-queue --allocator-only --timeout 120
# 子进程 exit 0，外层 exit 1 / BLOCKED
python tools/pure_linux/audit_linux_allocator.py --directory "$PRIVATE_COPY" \
  --result-sha256 "$RESULT_SHA" --output "$PRIVATE_AUDIT"
# exit 0
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0，1089 passed，1 skipped
```

引擎、程序与输出身份见[机器证据](pure-linux-native-allocator-evidence-20261029.json)。
私有数据保存在 guest `/tmp/nashura-linux-allocator-20261029-v1` 及本机
`/private/tmp/nashura-linux-allocator-20261029-v1`、`v2`；未发布原始日志、库或资源。
下一步定位工厂第二参数的实际构造和释放，按原始所有权建立持有者，并继续闭合
配置异常及 Backup 契约。ResourceManager、GameState、Step 和完整对局均未运行。
