# Four-deck mixed RL

The five-deck continuation that adds Royal Recruits / Royal Hogs is documented in [五卡组持续混合训练](recruits-hogs-training.md). This page records the original four-deck experiment.

Run: `event-mixed-deck-100r-20260920`. The learner lives on `.34`; every complete checkpoint is SHA-verified on `.37`. Other hosts retain reproducible inference caches. Training ends after at most 100 PPO updates or a Golem protection stop.

`mixed-deck.json` pins B0, RL 600, RL 645, the starting absolute optimizer iteration, and the action constraints. `B0.json` records the complete parent round. The model remains snapshot-only, with its residual MLP and original parameter shapes. AdamW state and absolute optimizer counters continue; the Golem-only reference-KL controller starts at 0.01 and is persisted separately.

## Curriculum and Sampling

The immutable registry is `native_runner/training/mixed_decks.py`. Cards use their base form, level 11 and Princess Towers; selectable ACT delays start at 100 ms. No held-out variants enter collection.

| Mixed updates | Golem | Giant | Hog | Royal Giant |
|---|---:|---:|---:|---:|
| 1-20 | 0.60 | 0.20 | 0.10 | 0.10 |
| 21-60 | 0.40 | 0.20 | 0.20 | 0.20 |
| 61-100 | 0.25 | 0.25 | 0.25 | 0.25 |

These are PPO loss weights. Each deck's valid decision rows are averaged independently. Advantages are standardized within each deck; policy, value and entropy use the same mixture. A separate `0.01 * mean(KL)` term initially anchors only Golem rows to B0, using identical saved action masks. Other decks contribute no reference KL. Chunked backward passes accumulate the full-round objective before one AdamW step. LR is 1e-5, one epoch, clip 0.2, entropy 0.01 and post-update KL threshold 0.01. The reference controller targets 0.02 and stays within 1e-4..0.1.

Every collection round has 108 games and at most 180 trainable lanes. The 72 self-play games cover each ordered deck pair four times, plus eight extras alternating by parity each round. Historical games form 18 swapped seed pairs, recording only the current model's side. Twelve games use B0/600/645 equally, always on Golem; 24 use the newest two eligible mixed checkpoints. A checkpoint must be ten updates older than the model collecting data: at mixed round N, the current model is N-1 and history is at most N-11. Thus rounds 1-11 fall back to B0 for the recent-history slot; round 12 has one eligible mixed snapshot.

`match-plans/iteration-*.json` freezes decks, owner identities, seeds, phase, loss weights and a canonical SHA. Shard preflight rejects identity, phase, deck or constraint mismatches and missing valid rows for any deck. `native-validation.json` reconciles execution receipts and terminal records before PPO. `update.log` and checkpoint metadata include deck row counts, collected proportions and learning weights. Native result statistics are grouped by own deck, opponent deck and opponent checkpoint.

## Evaluation and Protection

The same persistent inference service and native engine ports evaluate between updates. Evaluation workers save terminal scores and execution audits but no PPO shards. Each result identity includes both checkpoint SHAs, both decks, seed, owner, rules, constraints and runtime fingerprint. Completed games survive restart and are verified before reuse.

Before training, 16 native games cover the ordered deck matrix; then B0 plays the 360-game fixed baseline. Every ten updates repeats the matrix: 16 cells times ten swapped pairs, plus Golem against RL 600 and 645 with ten pairs each. Scores are `(wins + 0.5 * draws) / games`. Bootstrap resamples entire seed pairs, stratified by matchup; aggregates weight cells equally even after expansion. Golem mirror scores include the B0 mirror and the two fixed historical Golem matchups; cross-deck scores cover the other three B0 decks.

Stage checks at updates 20, 60 and 100 require the lower 95% paired confidence bound for both Golem score differences to reach -0.05. Uncertain evidence expands Golem cells through 20, 40, 80, 120 and 200 pairs. An intermediate point estimate below -0.05 also triggers expansion. Confirmed regression stops immediately at the boundary; a stage check still uncertain at the sample cap also stops. `last-qualified.json` retains the most recent protected checkpoint.

`selected-weights.json` selects the best protected candidate by equal-deck average. Independent seeds use 25 pairs per matrix cell. `not_yet_verified` explicitly denotes insufficient improvement evidence. The final checkpoint also faces mixed rounds 20, 50 and the best protected intermediate model, deduplicated by SHA, with four deck mirrors and 25 pairs each. Held-out evaluation tests only the four specified one-card replacements against B0's base decks, paired with B0 using those same variants. All result matrices and limitations are written to `final-report.json` and `final-report.md`.

## Operations

The deployed entry point is:

```sh
python3 -u /home/gujiga/firstlight_CR/runs/event-mixed-deck-100r-20260920/orchestrator-v2-code/tools/event_mixed_train.py --run-root /home/gujiga/firstlight_CR/runs/event-mixed-deck-100r-20260920
```

The entry point holds the parent and new-run orchestrator locks, preventing port overlap. `STOP` is honored at complete-round or evaluation-wave boundaries. Restart the same entry point after investigating failures. Complete rounds resume from verified checkpoints; interrupted collection is preserved under `interrupted-rounds/` and recollected. Stored round evaluations prevent skipping a stage gate after restart. The run records terminal protection decisions before final evaluation, so restart does not resume stopped training.

Do not mutate frozen runtime directories after activation. `manifest.json` is checked by collectors and learners. The version-two orchestrator fixes a cache-cleanup template collision in its own frozen directory, recorded in `fleet.json` and `orchestrator-v2-manifest.json`; the collector/learner runtime and evaluation fingerprint stay unchanged. Read `status.json`, `training-v2.log`, `evaluations/round-*.json`, and `iteration-*/success.json` for progress. Earlier startup and baseline logs remain in `training.log`. B0 using unfamiliar decks is an initialization baseline, not a mature specialist; this experiment tests recombination of known cards.
