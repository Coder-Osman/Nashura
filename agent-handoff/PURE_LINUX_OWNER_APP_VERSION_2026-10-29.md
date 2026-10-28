# E2.1-N：版本桥与对象获取的限定验证

基线 `588007f`。严格 `CATALOG_READY_PASS=false`，本阶段仅为
`RUNTIME_OBSERVED_LIMITED`。

## 证据与实现

此前 Linux 子进程在对象构造期间经 `0x140b250` 进入空 JavaVM 路径；新增有界
GDB 观测捕获该方法描述对象的名称 `getAppVersion`。匹配 DEX 的静态代码显示
`ApplicationUtilBase.getAppVersion()` 获取包管理器的 `versionName`，捕获
`NameNotFoundException` 后调用异常处理并返回空字符串。静态分析不证明异常分支
运行时发生过。

Android 只读独立实例自然启动中，构造作用域内的包装函数 `0x140cc8c` 返回
`15.535.13`，与已核验安装包版本一致；稳定复测前后均为 908 项 update 清单，
脚本错误和丢事件均为零。首次冷启动清单由 990 项变为 908 项，结果作为预热
保留，不作为身份稳定的证据。此观察不构成完整轨迹比较。

新增实验开关 `--owner-getter-only --app-version 15.535.13`：仅在隔离子进程中，
在 SHA 和入口指令校验后替换该版本包装入口，使用引擎原生字符串构造函数，检查
输出长度和全部字节。任何其他版本、未经指定的输入或联合 catalog/factory 运行
均被拒绝；此桥没有实现包查询失败或异常回调，故只覆盖这次已观察成功路径。
没有修改磁盘上的原始库、伪造 JavaVM 或写入 ready。

ARM64 Linux + Android Bionic guest 中，启用桥后原生构造入口实际调用并返回一次，
获取入口返回的对象可读、与缓存槽相同、虚表匹配；version 字符串字节校验通过。
本次观测中方法解析及 JNIEnv 获取候选各零次，范围仅限已挂点和 post-load 窗口。
GDB 与 native 进程退出 0，但外层仍为 `BLOCKED`，因为配置契约与 Android Backup
等价未通过。资源、引擎与辅助输入前后哈希不变。对象释放、自然初始化顺序及
数据库初始化尚未在该 Linux 实验中验证。

## 命令与身份

```sh
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# 预热及稳定运行 exit 0
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
  --native-backup-queue --owner-getter-only --app-version 15.535.13 \
  --observe-targets owner_jni_targets.json --timeout 120
# native/GDB exit 0；外层 exit 1、status BLOCKED
python tools/pure_linux/audit_owner_app_version.py --android "$ANDROID_RESULT" \
  --before-dir "$LINUX_BEFORE" --after-dir "$LINUX_AFTER" --output "$AUDIT"
# exit 0
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0；1082 passed，1 skipped
```

DEX SHA `c781c71b622aaee4c0c1b94f8ae0c9aa639e228d66fb929a0c9c4672a5f81aa6`；
其余身份见[机器证据](pure-linux-owner-app-version-evidence-20261029.json)。
私有观察位于 `/private/tmp/nashura-owner-app-version-android-20261029-v1`、`v2` 和
`/private/tmp/nashura-owner-jni-linux-20261029-v1`、`v2`、`v3`；guest 隔离输出在
`/tmp/nashura-owner-jni-linux-20261029-v1`。AVD 已关闭。

下一步需要从 Android 实际上游调用确定初始化对象、参数及数据库加载顺序，再在
Linux 中逐项验证；并行闭合配置异常及 Backup 等价。ResourceManager、GameState、
Step、完整对局、无 VM 生命周期验收和性能测试仍为 `NOT_RUN`。
