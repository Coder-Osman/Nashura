# E2.1-N：loading 配置文件映射与观察器作用域修复（2026-10-28）

基线：`92d211e406d84234586786817a9600b63b08dd22`，分支
`codex/pure-linux-libg-headless-20261026`。本次只读附加现有隔离 AVD，不写配置、不重启进程、
不进入 catalog 重跑或后续对象图。

## 结论

通过 Frida 16.7.19 在已核验的 PID 6901 中反射 `GameApp.c/o/p → c.a.d →
SharedPreferencesImpl.mFile/mBackupFile`，得到：

| GameApp槽 | 偏好文件 | 目标字面键 `LOADING_SCREEN_STATE_LABEL` 的 `contains` |
|---|---|---|
| `c` | `localPrefs.xml` | false |
| `o` | `storage.xml` | false |
| `p` | `storage_new.xml` | false |

`o/p` 是当前观察到的双槽文件对象；`c` 指向单独的 `localPrefs.xml`。
`localPrefs.xml` 当前不存在。`storage.xml` 和 `storage_new.xml` 各65字节，SHA256均为
`3325d2a819fdd8062c2cdc48a09b995c9b012915bcdf88b1cf9742a7f057c793`；对应 `.bak` 均不存在。
这与前次受控事务恢复空值后的文件状态相符。

`contains` 查询仅针对原始字面键。因原实现对键有编码，三个 false **不证明编码后的键不存在**。
路径探针没有枚举偏好内容或读取值；另一个只读探针调用原有 `getKeyValue`，观察其读取链。
文件元数据来自本阶段较早的只读采集，不是最后一次探针同时采集的快照。

**修正旧观察器的证据解释：** 上次 store 后同进程 API 读回成功，但旧观察器只有在参数包含
包名时才打开线程作用域。`storeKeyValue(key,value)` 的匹配过滤虽命中，却没有打开作用域，
导致内部 `c.a.n`、编码键操作及无参 commit 可能被过滤。旧事务的这些零计数现标为
`UNCOVERED_BY_SCOPE_FILTER`，不代表原版没有调用或没有落盘。
两次恢复后的 XML 哈希相同只证明末态相同，不能排除中间写入；跨进程恢复仍未验证。
原事务报告和原始日志保持原字节，本报告更正其解释，不补造历史调用。

## 观察器及事务入口修复

- 每线程作用域改为可嵌套的 Map；匹配 loading 入口也打开作用域，退出或异常时恢复父作用域。
- 补充 `getKeyValue`，使编码键读链在已匹配入口内可见。
- `commitToMemory` 返回对象通过 Java `IdentityHashMap` 与作用域关联，跨线程写盘按同一个
  `MemoryCommitResult` 的对象身份归因，完成时释放。最多保留256个关联；超限、观察失败及
  未关联写盘显式记为未覆盖或未知，不伪装零次。
- 真实写入入口默认只读，必须显式指定 `--execute-store`。写前捕获真实原值，目标写入的异常、
  读回失败、日志故障或中断均进入恢复 finally；恢复失败明确标记并保留记录，不重复自动写入。
- 输出采用 exclusive 创建、逐条 flush 和线程锁，失败不覆盖既有记录；Frida 使用精确设备 ID。

异步写盘关联目前仅通过 mock 验证，尚未在修复后的真实写入中验证。方法安装成功并不表示
其调用边已运行；本阶段没有第二次真实 store、commit 或 Backup 请求。

## 实际验证

旧版观察器在新的嵌套 fixture 上退出1，断言 `c.a.n` 进入次数为0而非1；该预期失败复现了
漏记原因，不是游戏引擎故障。新版8个JS mock场景与10个Python事务恢复用例合计18项通过。
Mock覆盖嵌套、交错线程、异步关联、异常、关联上限及观察器故障，不当作Android写盘证据。

修复后两轮真实只读运行均exit0，各50条记录。最终轮观察到：

| 方法 | 进入次数 | 证据级别 |
|---|---:|---|
| `ApplicationUtilBase.getKeyValue` | 1 | `RUNTIME_OBSERVED` |
| `c.a.j` | 2 | `RUNTIME_OBSERVED` |
| `SharedPreferencesImpl.contains` | 2 | `RUNTIME_OBSERVED` |

33个hook安装、2个observer ready；无 `call_throw`、`observer_error` 或不可用hook。
本轮未调用store，commit/写盘/Backup不属于本轮运行覆盖，不能从其零计数作存在性推断。
游戏PID6901和system_server PID522在结束核验时保持原值。

## 动态与失败记录

- `RUNTIME_OBSERVED`：Frida 根据应用标题显示 `Null’s Royale`，而 ADB `/proc/6901/cmdline`
  核验包名为 `nullsroyale.rel.free`；runner 已调整为使用 PID 与 `/proc` 身份核验。
- `RUNTIME_OBSERVED`：字段反射沿运行时类继承链找到父类声明的 `c/o/p`。
- 保留失败：初版 runner 因 Frida 显示名与包名不同而退出1；初版反射只查子类字段而报
  `NoSuchFieldException`；修正后只读运行退出0。
- 保留失败：一次 `su sh -c` 元数据循环命令有 shell quoting 错误，退出1；改用直接 `ls`
  和 `sha256sum` 后成功读取。不存在文件报告为缺失，不据此推断其他路径。
- AVD `emulator-5580` 的游戏进程在结束时仍为 PID 6901；没有停止、重启或清数据。

## 命令与证据

新增只读工具：`tools/pure_linux/preferences_paths_readonly.js`、
`tools/pure_linux/run_preferences_paths_readonly.py`。工具只反射文件路径及查询字面键存在性，
不写值、不读取 XML 内容、不调用 commit。

实际命令：

```sh
node --check tools/pure_linux/backup_runtime_observer.js
node --check tools/pure_linux/preferences_paths_readonly.js
# exit 0

/opt/anaconda3/bin/python3 -m py_compile tools/pure_linux/run_loading_store_once.py tools/pure_linux/run_preferences_paths_readonly.py
# exit 0

/tmp/nashura-frida-16.7.19-venv/bin/python \
  tools/pure_linux/run_preferences_paths_readonly.py \
  --script tools/pure_linux/preferences_paths_readonly.js \
  --output /private/tmp/nashura-loading-scope-final-20261028-sLK5i5/preferences.json \
  --pid 6901 --adb "$HOME/Library/Android/sdk/platform-tools/adb" \
  --serial emulator-5580
# exit 0

/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0；549 passed，1 skipped，6.31s

/tmp/nashura-frida-16.7.19-venv/bin/python \
  tools/pure_linux/run_loading_store_once.py \
  --observer tools/pure_linux/backup_runtime_observer.js \
  --output /private/tmp/nashura-loading-scope-final-20261028-sLK5i5/frida-events.jsonl \
  --game-pid 6901 --system-server-pid 522 \
  --adb "$HOME/Library/Android/sdk/platform-tools/adb" \
  --serial emulator-5580 --observe-only
# exit 0；真实只读，未调用store

git show HEAD:tools/pure_linux/backup_runtime_observer.js | \
  node tools/pure_linux/backup_observer_scope_fixture.cjs /dev/stdin nested
# 基线92d211e上的旧代码exit1，预期复现漏记

/opt/anaconda3/bin/python3 tools/run_main_engine_checks.py
# exit 0；211 passed，22.45s
```

最终 JSON 留在本机私有目录；SHA256：
`f007e0238473ab2f04142a152e43b67d35c3a7c1f152ca735535ace194471b1b`。
只读事件日志SHA256：`ef7914608566a3151ff14eb197307ed62644aacfe48be3f8cd8f9c74264057fa`。
最终源码SHA及独立发布检查结果记录在机器摘要中；早期探针SHA不冒作修复后的身份。
文件元数据与哈希命令针对真实 AVD 路径，两个现存 XML 的 SHA 如上；没有将 XML 或私有运行日志复制进仓库。

机器可读摘要见 `pure-linux-loading-store-paths-evidence-20261028.json`。

## 发布检查

公开回归在独立源码副本中执行 `tools/run_public_checks.py`：exit0，1104 passed、42 skipped，
171.98秒。副本用于隔离测试生成的XML，原仓库248份历史XML与基线逐字节相同。
公开回归日志SHA256：`f48abe2cec7a46c9081e0d388219b52242913dc06c4dd13767b3b04410663de1`。
`gitleaks dir . --config .gitleaks.toml --redact` exit0，未发现凭据。

保留发布检查失败：更新README、RESULTS和CHANGELOG后，源码清单检查exit1，报告
`source manifest mismatch: CHANGELOG.md`。按仓库维护规则保留原始来源SHA，更新公开字节
SHA并登记本轮仓库内维护原因；早先2477文件通过的结果仅属于文档更新前，不冒作最终检查。
最终清单纳入本阶段十份源码与证据，2487个文件校验通过，exit0。

工程回归、mock和真实只读观察分别记录，均不替代受控写入与原生catalog完整验收。

## 门状态与下一步

| 项目 | 状态 |
|---|---|
| `GameApp.c/o/p` 与 `mFile/mBackupFile` 映射 | `RUNTIME_OBSERVED` |
| 双槽真实文件身份 | `RUNTIME_OBSERVED`，当前两文件内容哈希相同 |
| 原始字面键直接存在性 | `RUNTIME_OBSERVED`，三个实例均 false；编码键身份未知 |
| SharedPreferences commit 与磁盘事务 | `UNRESOLVED` |
| 旧事务commit零计数 | `UNCOVERED_BY_SCOPE_FILTER` |
| 修复后的跨线程写盘归因 | `UNIT_PASS`，真实写入 `NOT_RUN` |
| Backup journal 的新增/去重语义 | `UNRESOLVED` |
| 重启后值恢复 | `NOT_RUN` |
| `CATALOG_READY_PASS` | `BLOCKED`，未重跑 catalog |
| ResourceManager、GameState、Step、no-VM、Android-free、10×、训练 | `NOT_RUN` |

下一项需要再次受控地写入一个临时值并恢复原值，且观测编码键对应 editor、commit、
磁盘 write/rename/fsync 与恢复读回。前次唯一获准的 store/restore 已用完；开始新的写入前需
新的明确许可。可先继续不写配置的静态分析与现有记录审计。
