# Roadmap

The steps still to do, from the end of the Stage 9 baseline run (2026-10-30) to the Stage 12 comparison. `progression.md` records what was built and found. This file records what comes next and the decisions already made about it. When a step is done, remove it here and write it up in `progression.md`.

Working rules for every step: the code is written from skeletons with numbered TODOs (for example `TODO(12a.1)`), reviewed by running it, and every run that spends API money needs an explicit go-ahead.

## Where things stand

- **Stage 9 baseline:** 344 of 500 resolved (68.8%, 95% interval 64.6% to 72.7%). On the held-out 360 it is 249 (69.2%). Run `2026-10-29T09-34-15`, details in `progression.md` Steps 9f and 9g.
- **Data split (Step 9a):** `instances/heldout_360_seed1001.txt` is the comparison set and is not read or tuned on until Stage 12. `instances/pool_140_seed1001.txt` (which includes dev_25) is the only data that Stage 10 design and Stage 11 tuning may look at.
- **Measured costs:** about $0.010 and 124 s per instance (89 s of it agent time) on the Mac, sequential, with `--score-each --remove-images`.
- **Started early:** Step 12a (sharding). `shard_instances.py` and `merge_scores.py` are done, and a stubbed two-shard run passed (`progression.md` Step 12a). Only the real shard timing pilot is left.
- **Stage 9 is complete.** Steps 9h and 9i made `--score-each` evaluate each instance once and a stop print the full error messages (`progression.md` Steps 9h and 9i).
- **Also started early:** Step 12b, the analysis rules, is written (`progression.md` Step 12b).
- **Stage 10 is complete.** Arm 2 is `config/narration_fix.yaml`, frozen at Step 10c (`progression.md` Steps 10a to 10c).
- **Step 11a is complete.** `gepa==0.1.4` is confirmed and pinned, with the workarounds listed under Stage 11 (`progression.md` Step 11a). Next is Step 11b.

## Stage 10: Narration fix (arm 2)

The Step 6b problem is still open: a first reply often narrates a whole session in several bash blocks, the format check rejects the reply, and a run whose edits were all in rejected replies can submit an empty patch (django-11333 in Step 8e).

- **10a. A narration measure (done).** `narration_measure.py`, with results in `progression.md` Step 10a. About 19.5% of steps are lost to rejections. `lost_share` is arm 2's primary metric, as a check of the mechanism.
- **10b. The fix (done).** `config/narration_fix.yaml` turns on `narration_fix`: the first block of a multi-block reply runs, and an exit with no change is refused (`progression.md` Step 10b). Checked on scripted replies only. Not now: accepting repeated identical blocks, and native tool calling (the next step if arm 2 fails).
- **10c. Check on dev_25 (done).** `lost_share` fell from 17.4% to 7.2%, and resolved was 36 of 50 against 34 of 50, within noise (`progression.md` Step 10c). `config/narration_fix.yaml` is frozen. The refused exit was never used, so it is unmeasured on the real model.

## Stage 11: GEPA (arm 3)

- **11b. The adapter** (`gepa_adapter.py` and a driver). `evaluate` runs `run_and_save` then `run_harness` and `collect_scores` per instance, each `evaluate` in its own run subfolder. It starts from `config/narration_fix.yaml`'s values and leaves that file unchanged. Unit test with a stub agent and a stub harness. Step 11a added these requirements:
  - **Template check before any container starts.** A candidate scores 0 unless both templates format, the task template keeps `{problem_statement}`, and the system template keeps the `bash-action` block.
  - **A reflection client with `total_cost`,** saved to disk and reloaded on resume. A plain callable reports $0, so `max_reflection_cost` would never fire, and a new client would reset the total.
  - **A rollout cache on disk,** keyed by candidate hash and instance id and written after each instance. Without it, each resume repeats the 40 seed rollouts.
  - **An `agent_error` retries once and then stops the run,** because a 0 there would corrupt the acceptance test.
  - **A custom `reflection_prompt_template`,** because the default asks for instance-specific facts.
- **Decisions already made:**
  - GEPA starts from the arm 2 prompt and optimizes once. Arm 3 is arm 2 plus the GEPA templates, so arm 3 minus arm 2 isolates GEPA.
  - It changes `system_template` and `task_template` only. Steps, limits and model stay fixed.
  - Metric: 1 if resolved, else 0.
  - Feedback aims at incomplete fixes: a computed failure label (`incomplete_no_f2p`, `incomplete_partial`, `regression_only`, `other`), full detail only for unresolved instances, F2P and P2P counts with failed test names, a short excerpt of the failing assertion, the size of the gold patch with none of its content, a truncated patch and the last agent turns. No narration counts (`progression.md` Step 11a).
  - Split within the pool: val 40 (random, seed recorded), train all remaining baseline failures (about 32) plus the same number of random resolved ids, about 64 in total. The pool has only 45 failures, so the earlier 100 with half failures was not possible. Train and val images stay cached during the run. The dev_25 images are kept for this, and can be removed after 11d.
  - The GEPA val score is never reported as a result (winner's curse on 40 noisy instances). Only Stage 12 numbers are.
  - Reflection model: `gpt-6.1-sol` at the default `medium` effort, with `gpt-6.1-terra` as the cheaper fallback.
- **11c. Smoke run:** val 3, train 6, `max_metric_calls=20`, about $0.30 (ceiling $2). This is the first check of the reflection model's proposals.
- **11d. Full run:** `max_metric_calls=300`, which can run up to 45 rollouts over because the budget is checked between iterations. About 14 to 26 iterations, depending on how many proposals are accepted. About $3.50 in rollouts plus reflection capped by `max_reflection_cost` ($10), about 10 h sequential. A resumed run is recorded as resumed, since it does not reproduce an uninterrupted one.

## Stage 12: Three-arm comparison on the held-out 360

- **12a. Sharding (scripts done, one check left).** `shard_instances.py` splits an ids file into shards of whole (repo, version) groups, each shard runs as its own `run_batch.py` process into its own folder, and `merge_scores.py` merges the scores with checks.
  - Give each shard its own `--output-dir`. Shards started in the same second get the same run folder name (`progression.md` Step 12a).
  - **Real shard timing pilot.** Needs its own go-ahead and must not overlap another batch. It is the only check of two shards running at the same time (Docker images, disk, rate limits).
- **12b. Analysis rules (done, written early).** Fixed in `progression.md` Step 12b before any arm results: the comparisons, the outcome, the exact McNemar test and paired bootstrap interval, the second-run rule and the secondary outcomes. A change must be made before the Stage 12 runs start and recorded there with its date and reason. The analysis script is checked on Stage 8 data first.
- **12c. The runs.** Baseline, arm 2 and arm 3, one run each on all 360, plus one repeat baseline run to measure run-to-run noise on this set. Arms are interleaved or run in parallel so time-of-day and API drift affect all of them. Analysed by the rules of 12b.
- **Open before the runs start:** GEPA can return arm 2's own templates, because ties keep the seed. Step 12b has no rule for arm 3 being identical to arm 2. Any rule is recorded in Step 12b with its date and reason.
- **Spend and time:** about 4 × $3.60 ≈ $14.50 (ceiling about $115), and about 4 × 360 × 124 s ≈ 50 h of machine time sequential, so less with shards. Each run needs a go-ahead.

## Spend still ahead (each needs a go-ahead)

| Step | What | Typical | Ceiling |
|---|---|---|---|
| 11c | GEPA smoke run | $0.30 | $2 |
| 11d | GEPA run | $3.50 + up to $10 reflection | $34 |
| 12a | shard timing pilot | under $1 | to set at the time |
| 12c | 4 runs on 360 | $14.50 | $115 |
