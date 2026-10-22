# Live match dashboard

Start from the repository root with `.venv-live/bin/python -m live_web --open`,
or double-click `Open-Live-Match.command`. Both launch the unified CR workbench;
the live page is http://127.0.0.1:8765/live/ and the video page is
http://127.0.0.1:8765/. Header tabs switch between them. The former standalone
server is retired; the live launcher reuses the video workbench's server.

## 原生人机友谊战（双模拟器）

工作台“人机对战”入口 `/play` 进入 `/live/?mode=friendly`。采用已经安装游戏的两个
AVD：`firstlight-live` / `emulator-5570` / **Nashura** 为 AI，
`firstlight-human` / `emulator-5572` / **HumanPilot** 为人类。两个独立账号同时在线，
均在 `hashura` 部落；沿用客户端和账号数据，无需另装 APK。

1. 保持两个游戏窗口打开，选好双方游戏内的卡组。
2. 网页选择“人机友谊战 · 原生操控”和模型，点击“准备 AI，等待友谊战”。
3. 出现“等待友谊战”后，在 Nashura 窗口的 Social → Friendly Battle → 1v1 发起挑战。
4. 在 HumanPilot 窗口进入同一部落聊天，接受该挑战。
5. **直接在 HumanPilot 模拟器窗口点击或拖动卡牌**。AI 只控制 Nashura，网页画面显示
   AI 视角，作为观测面板使用。收到原生 `bound` 事件后，网页显示“对战中”。

友谊战模式保留当前游戏页面，不自动导航大厅或点击普通 Battle；等待上限为 15 分钟。
正常结束或点击“停止接管”只结束本局的实时状态读取与 AI 进程，两台模拟器都保留。
下一局先返回游戏大厅或 Social，重新点击“准备 AI”后再发起挑战。
AI 进程就绪前请勿进入战斗；AI 需要从开局读取本局实时游戏状态，才能基于当前手牌、圣水、单位和塔血出牌。
这条状态流只供当前友谊战推理和故障诊断，不会被导出为训练样本、触发评估或更新任何权重。

若 HumanPilot 窗口已关闭，启动已有 AVD（不创建或覆盖账号）：

```sh
"$HOME/Library/Android/sdk/emulator/emulator" -avd firstlight-human -port 5572 \
  -cores 4 -memory 4096 -gpu host -no-snapshot-load -no-snapshot-save -no-boot-anim
```

已有 `emulator-5572` 在线时复用其窗口，不重复执行启动命令。
AI AVD 仍由工作台原有启动流程准备。

2026-09-28 实测：用户直接操作 HumanPilot，AI 对 Nashura 完成 20 次出牌，20 次均取得原生
确认。任务 `20260928T151506-9f8c89b1` 正常终局，42 次决策、14 次未提交计划取消，
实时状态读取无 read/script/hook error。验收资料在 `artifacts/native-friendly-20260928/`。
这验证交互链路，不作为策略胜率评估。

## AI 普通匹配

选择“AI 普通匹配”或打开 `/live/?mode=standard`。Select a checkpoint and start one match. The dashboard prepares the dedicated
`firstlight-live` AVD (`emulator-5570`), starts the existing Frida server if
needed, captures public observations, loads the selected model and taps the
verified Battle button. Recognized result and battle-log screens are closed
automatically. Other unrecognized screens produce an actionable error without
starting a match. The current in-game deck is used.

The window includes a live screenshot preview, own elixir and hand, decisions,
confirmed deployments, stop control and persisted match history. Stopping
releases AI control; it does not abandon the game or close the emulator.
Normal completion stops capture and inference, and keeps the result screen.
Repeated starts are rejected, including competing CLI policy runners.

## Checkpoints and prerequisites

Remote checkpoints share the video workbench's SQLite catalog, IDs, SSH
connection configuration and refresh worker. There is one startup refresh and
one refresh every five minutes, and either page's refresh button invokes that
same worker. Remote video generation uses the original archive on the GPU
machine. Live play downloads only its selected checkpoint to
`artifacts/render-web/weights/<sha256>.pt`, verifies SHA256 and the local model
contract, then reuses that file on subsequent starts. A stopped or failed
transfer never publishes a partial weight or starts game matching. Downloads
show progress and are cancellable. Both pages retain the shared catalog when
a remote refresh fails.

Local `.pt` files under `checkpoints/` also appear after metadata checks. The folder
button adds an existing local `.pt` or `.pth` path without copying its weights.
Stateless v2/v3 event contracts are accepted; legacy or unfinished migration
weights are excluded. The runner verifies complete model loading before the
Battle button is pressed. Requests are restricted to localhost and writes
require the dashboard's control header and same-origin request.

Runtime dependencies in `.venv-live`: PyTorch, Frida, Pillow, FastAPI,
Paramiko and Uvicorn. Live subprocesses use this environment even when the
unified server uses the video workbench's Python. The existing AVD, supported
client and Android SDK must be installed.
Startup verifies ADB root before reading the game's `/proc/<pid>/maps`, then
requires five seconds of a stable foreground game PID with `libg.so` mapped
before preparing Frida. Preview and `pidof` can work without these read
permissions; permission failures are reported directly instead of waiting for
the 75-second game-stability timeout. Existing Frida servers also require the
ADB permission check after a device restart.
Frida starts detached from the ADB shell and gets up to 45 seconds to become
ready. Readiness uses a bounded Frida API call in `.venv-live`, not only a PID
check. An existing responsive server is reused; an existing unresponsive server
is reported without starting a duplicate. Stopping cancels the readiness wait.
Startup errors include the guest log at `/data/local/tmp/cr-live-frida-server.log`;
the temporary readiness port forward is always removed.
The required engine and calibration files are in
`artifacts/live-null-20260920/`: `libg.so`, `calibration-01.jsonl` and
`calibration-02.jsonl`. Navigation templates are bundled in `live_web/templates`.
Game changes or a different resolution require rechecking these templates and
the touch calibration. Current resolution is 1080 x 1920.

This retains the existing partial real-client observation adapter and its
base-form / Princess-tower assumptions. Model memory remains disabled, and
opponent private hand, deck and elixir are not used. Match results here do not
replace controlled strength evaluation. See `native_runner/live/README.md`
for the current observation and event-scheduling limitations.

## Artifacts and checks

Each match writes `artifacts/live-web/jobs/<id>/` with `summary.json`,
`capture.jsonl`, `policy.jsonl`, process logs, touch receipts and, when
available, `result.png`. `capture.jsonl` is the implementation's transient
real-time state stream and `policy.jsonl` is the AI decision audit. Neither is
a training dataset: the dashboard has no trainer, evaluator, optimizer, or
weight-writing path. The dashboard never removes existing artifacts.

```sh
.venv-live/bin/python -m pytest live_web/tests/test_live_web.py \
  native_runner/tests/test_live_snapshot.py native_runner/tests/test_live_adapter.py -q
```

`live_web/tests/browser_check.cjs` exercises checkpoint selection, invalid
imports, history and desktop/tablet/mobile layout against the running server.
It uses Playwright; set `CR_LIVE_TEST_BROWSER` to an installed Chromium binary
if required. Its optional `--match` flag deliberately starts one real match.
