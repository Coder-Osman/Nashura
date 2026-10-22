# Partial real-client integration

This module connects the stateless event policy to a visible Null's Royale
client. It is an integration diagnostic, not a complete training observation
implementation or a policy-strength benchmark.

## Components

- `capture.py` verifies binary/resource fingerprints and starts `reader.js`
  with `phase-reader.js` and `timing-reader.js`. The public card-play feed observes the native card
  consumption function, including spells. It reads no opponent hand, deck,
  elixir or pending commands. Ordinary unit spawns are not card-play events.
- `adapter.py` maps own hand/elixir and visible entities to the FAIR contract.
  `runtime.py` passes captured phase facts through the training resolvers.
  Destroyed towers remain in the observation and update crowns/placement.
- `tracker.py` derives public opponent elixir bounds and revealed-card cycles.
  Unseen cards and uncertain cycles remain unknown; special resource mechanics
  widen resource bounds. This is explicit public bookkeeping, not neural memory.
- `policy.py` resets recurrent state on every decision, uses the training FP16
  observation roundtrip and temperature-1 sampling, and allows cards affordable
  within a legal delay. A uint32 match seed and four random draws per decision
  make sampling reproducible. Logs include ACT and selected-delay probabilities.
- `scheduler.py` replaces a pending plan after any new native card play, and
  wakes WAIT after 300 ticks (15 seconds). ACT click deadlines are advanced by
  1000 ms, clamped to the source observation tick. The original sampled delay
  is retained. There is no extra full-elixir timer.
- `run.py` keeps reading events while a dedicated touch worker waits for a
  receipt. Before the target touch, new events cancel the transaction. After
  the touch is committed, the runner awaits its native own-card receipt; an
  uncertain committed touch stops the match controller without retrying.
- `input.py` builds `CRLiveInput.java` with the local Android SDK/JDK. One ADB
  connection per match injects ordinary Android touchscreen events; EOF closes
  the helper. Hand rotation is logged but is not required for confirmation.
- `projection.py` uses measured touch/spawn calibration. Current calibration
  has a maximum residual of 20.51 pixels, so placement is approximate.
- `latency.py` reports decision-to-click separately from native consumption
  and sampled deployment state. A report is saved as `latency.json` after each
  policy process exits; it can also be recomputed from that job's logs.

## Running

Use `.venv-live/bin/python`, the dedicated `emulator-5570`, a matching Frida
server forwarded to host port 27742, and the supported client build. Capture
and receipt files are opened exclusively; choose a new output name each run.
Start capture before the policy runner, then enter a match in the GUI.

```sh
.venv-live/bin/python -m native_runner.live.capture \
  --serial emulator-5570 --seconds 600 \
  --libg artifacts/live-null-20260920/libg.so \
  --output artifacts/live-null-20260920/capture-NEW.jsonl

.venv-live/bin/python -m native_runner.live.run \
  --capture artifacts/live-null-20260920/capture-NEW.jsonl \
  --output artifacts/live-null-20260920/policy-NEW.jsonl \
  --checkpoint checkpoints/live-rl711.pt --serial emulator-5570 \
  --calibration artifacts/live-null-20260920/calibration-01.jsonl \
                artifacts/live-null-20260920/calibration-02.jsonl \
  --seconds 420 --allow-partial --public-tower-identities
```

The runner defaults to shadow inference. Add `--execute` for actual touches.
Both modes use the compensated schedule. `--input-lead-ms 0` restores the
previous click timing; the default is 1000 ms, including dashboard matches.
Use `--seed 123` to reproduce policy sampling; omit it for a random match seed.
Actual input requires Android Studio's JDK and the local SDK/build-tools.
The helper jar is cached by source SHA in `artifacts/live-input/`.
The tower flag requires exact public runtime identities from the supported
client build. `--verified-princess-towers` remains a legacy CLI alias; it no
longer overrides observed tower identities. Standard and Royal Chef towers
are recognized. Unrecognized tower/entity identities still block inference.
Only base card forms are supported. Stop capture after the match; leave the
emulator open when interactive viewing is wanted.

## Known observation and scheduling limits

Some attack/deployment timers, visible targets and effective movement speeds
are available. Complete attack/movement phases, spell/effect/ability state,
card levels and tower activation/cooking state remain incomplete. In the
acceptance match, no validated attack-start/release edges or same-tick movement
delta were observed; those phases remain unknown. An attached hook alone does
not prove complete phase coverage. Sampling is non-atomic and drops frames
when the tick changes during a read. Hook failures are reported as `hook_error`.

Input arrives through a roughly 100 ms capture interval. Already submitted
client input cannot be withdrawn; only plans that have not committed a target
touch can be cancelled. The acceptance match still had about 1.1 seconds from
target touch to native card consumption. The current runner compensates by
advancing click deadlines by 20 ticks; it does not change the native command
window or unit deployment timers. Delays at or below 1000 ms become due as
soon as inference and snapshot checks permit, so their effective consumption
times converge near the native one-second floor plus controller overhead.
New public plays still cancel uncommitted touches; an earlier click commits
the action sooner. Hand, elixir, placement and spell-target checks still apply.
If the advanced play is not yet affordable, it is cancelled and reconsidered
through the existing recovery path. No forced full-elixir play is applied.

The cumulative public feed is bounded at 2048 events and checked for missing,
mutated or cross-match history. Missing events block play instead of falling
back to spawn guesses. Capture errors cancel pending plans, and continuous
blocking after the match binds stops the controller after 15 seconds.

After a target touch has committed, a transient `read_error` pauses receipt
observation within the original three-second deadline. It never resends the
touch or reuses the snapshot preceding the error. Observation resumes only
with a valid same-battle snapshot no older than 500 ms; script failures,
invalid snapshots and battle changes still stop the touch worker. The receipt
log records `receipt_observation_wait` and `receipt_observation_resumed`.
Entity sampling copies the collection's member pointers and checks them again
before publishing. Changed collections and duplicate entity IDs are discarded;
duplicate diagnostics retain both indices, pointers and names. These checks
detect additional read races but do not make sampling atomic.

The dashboard creates fresh capture/policy processes per match, so source
changes apply on the next match without restarting video tasks or training.

## Precise input timing

Run `python -m native_runner.live.latency artifacts/live-web/jobs/JOB` to
recompute a job's report. Only matches started with the timing instrumentation
have the complete trace; missing markers are reported rather than estimated.

- `policy.jsonl`: `timing.decision_finished_monotonic_ns` marks the return of
  inference, including diagnostic probability computation. `selected_delay_ms`
  stays separate from the observed delay until the touch worker is dispatched.
  Decision and touch records also retain `policy_scheduled_tick` (original
  deadline), `scheduled_tick` (compensated click deadline), `input_lead_ms` and
  `scheduled_delay_ms`. Older timing logs are treated as uncompensated.
- `policy-touch-NNN.jsonl`: hand and target DOWN/UP injection each have Android
  timestamps before and after InputManager dispatch, plus host send/ACK times.
  The helper performs five clock exchanges at startup and records the shortest
  round trip's offset bounds. Android and Mac monotonic clocks are not assumed
  to have the same origin. Cross-clock values report calibration uncertainty;
  within-device intervals require no offset estimate.
- `capture.jsonl`: `latency_marker` is an independent diagnostic stream. It
  observes verified own outbound commands, own native consumption, and the
  local battle's synchronization state.
  It never feeds pending-command data into the model, changes the public event
  schema, or modifies command timestamps. `own_command_created` means a fully
  constructed card command entered local submission (`c893d0`), not allocation.
  `own_command_outbound` marks entry into local outbound-message assembly
  (`c89778`); it does not mean server receipt. Consumption is marked on entry
  and return from `f38a68` and joined to the public receipt sequence.
- `battle_sync_state` samples local/synchronization ticks at most once per
  second, plus stop-state transitions even when the simulation tick is frozen.
  `battle_sync_fault` captures the first native stop branch at `106482c`,
  distinguishing clock lead, checksum mismatch during a step, checksum mismatch
  on receipt of a sync update, and explicit out-of-sync. Checksum faults include
  both compared values and their tick. Call-site/register layouts and the hook
  are pinned to the supported binary; no received command payload is inspected.
  These markers do not enter policy observations or authorize resubmission.
  `battle_stopped` names the legacy diagnostic field at battle `+0x1e9`; it is
  a synchronization stop flag, not the world's terminal flag. Old logs without
  a first-fault marker establish the blocked state, not which branch set it.
- Entity appearance/deployment timers remain sampled observations. A command
  consumed while its unit still has deployment time remaining is NOT treated
  as a completed deployment. Spell projectile/impact times are not inferred
  from this troop-only coverage.

The primary metric is **decision completion to target DOWN**, with target UP
also reported. This includes the model's remaining scheduled wait. The report
separately shows **dispatch to target DOWN** (execution overhead),
**target DOWN to command creation**, and **command creation to consumption**.
Injection boundaries measure Android input delivery, not screen presentation.
The earlier approximately 1.1 seconds from target touch to consumption occurs
AFTER the click and must not be counted as decision-to-click latency.

A committed touch may still be awaiting consumption when that same battle
ends. `TouchEnded` preserves the native terminal snapshot: the runner records
`unconfirmed_at_terminal` and then `terminal`, retaining the actual result and
confirmed-play count. It does not resend the input. Native confirmation takes
precedence when present; another battle's snapshot and other committed-touch
errors do not enter this completion path. The latency report lists these
touches in `terminal_unconfirmed` without inventing consumption times.

## Consistency repair acceptance, 2026-09-20

Match `20260920T111638-5793c218` used the original RL1175 checkpoint and reached
a real terminal at tick 3538. All 10 submitted cards had native confirmations;
8 pending plans were cancelled on actual opponent plays, including The Log.
No ordinary spawn caused cancellation. Median target-touch time was 22 ms,
with target start 0–1 ticks after schedule. Evidence and offline replay script:
`artifacts/live-consistency-fix-20260920/`.

This verifies the integration paths exercised in that match. It does not
establish full input equivalence or a measured win-rate improvement.

## Tombstone inference fix, 2026-09-20

In `rl711-max-01.jsonl`, Mega Minion and The Log were confirmed before
Tombstone entered the hand. The adapter omitted `footprint_width_tiles` and
`footprint_height_tiles` from placement metadata, triggering
`stationary building candidate lacks an exact footprint` in the full
tensorizer and blocking every later decision.

The adapter now forwards both footprint dimensions. Regression coverage in
`test_affordable_building_passes_full_tensorizer` traverses the complete
tensorizer, in addition to the existing placement-mask checks. All 30 live
snapshot/adapter tests pass.

Live verification: `artifacts/live-null-20260920/rl711-max-03.jsonl`, with raw
observations in `capture-11.jsonl`. The match reached its terminal state at
tick 1042 with 29 decisions and 5/5 confirmed deployments: Night Witch,
Tombstone, Elite Barbarians, Mega Minion and The Log. The result screen
`max-03-result.png` confirms a 3-0 win. The footprint error did not recur.
One transient capture read error recovered; 19 pending plans were cancelled
by public spawn events. This single match verifies the fix, not overall
playing strength.

## Royal Chef tower identity fix, 2026-09-20

Dashboard match `20260920T101254-2b17fb8b` stopped before any decision because
the adapter treated `ChefTowerKing` (1471932914) as a normal unit archetype.
Its side towers, `ChefTower` (3191249505), also need crown-tower routing.
`towers.py` now shares exact ID/name recognition between observation mapping,
placement occupancy and public-spawn scheduling. The Chef king uses tower
troop 159000004; its side towers use 159000000, matching the existing offline
contract. Observed HP, position and ownership are preserved; uncaptured Chef
cooking state remains unknown. No model vocabulary or checkpoint is changed.

Both kind 12 and kind 13 occur for the same live crown-tower identity, so the
helper accepts those observed states. Unknown IDs, conflicting names and
unsupported kinds still block inference. The recorded first eligible frame
is the regression fixture `tests/fixtures/live-chef-towers.json`.

The live adapter/snapshot/delay/dashboard suite passes 71 tests. Offline
re-inference of the failed capture with its original RL 1268 checkpoint
passes all 182 observation frames and all 153 frames eligible for inference.
Every inferred opening action is WAIT; no device input was sent by this
validation. Evidence: `artifacts/live-chef-fix-20260920/capture-verification.json`.
The already running dashboard starts a fresh policy child for each match,
so the next match imports the fix without restarting active video jobs.
