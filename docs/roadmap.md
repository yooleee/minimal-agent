# Roadmap

The steps still to do, from the end of the Stage 9 baseline run (2026-10-30) to the Stage 12 comparison. `progression.md` records what was built and found. This file records what comes next and the decisions already made about it. When a step is done, remove it here and write it up in `progression.md`.

Working rules for every step: the code is written from skeletons with numbered TODOs (for example `TODO(12a.1)`), reviewed by running it, and every run that spends API money needs an explicit go-ahead.

## Where things stand

- **Stage 9 baseline:** 344 of 500 resolved (68.8%, 95% interval 64.6% to 72.7%). On the held-out 360 it is 249 (69.2%). Run `2026-10-29T09-34-15`, details in `progression.md` Steps 9f and 9g.
- **Data split (Step 9a):** `instances/heldout_360_seed1001.txt` is the comparison set and is not read or tuned on until Stage 12. `instances/pool_140_seed1001.txt` (which includes dev_25) is the only data that Stage 10 design and Stage 11 tuning may look at.
- **Measured costs:** about $0.010 and 124 s per instance (89 s of it agent time) on the Mac, sequential, with `--score-each --remove-images`.
- **Started early:** Step 12a (sharding). `shard_instances.py` and `merge_scores.py` are done, and a stubbed two-shard run passed (`progression.md` Step 12a). Only the real shard timing pilot is left.
- **Stage 9 is complete.** Steps 9h and 9i made `--score-each` evaluate each instance once and a stop print the full error messages (`progression.md` Steps 9h and 9i).
- **Also started early:** Step 12b, the analysis rules, is written (`progression.md` Step 12b). Next is Stage 10a.

## Stage 10: Narration fix (arm 2)

The Step 6b problem is still open: a first reply often narrates a whole session in several bash blocks, the format check rejects the reply, and a run whose edits were all in rejected replies can submit an empty patch (django-11333 in Step 8e).

- **10a. A narration measure, no API.** A script over trajectories that counts, per run: replies with other than one block, format errors, `PrematureExit`, steps lost to rejections, and empty patches where a rejected reply contained an edit. Run it on dev_25's six runs and on the pool part of the Stage 9 run. It sets the primary metric for arm 2 before any change. Check it against hand counts on two known trajectories, including django-11333 in `2026-10-27T18-03-48`.
- **10b. The fix (decided).** Two changes that act as one intervention:
  - Run the first block of a multi-block reply and tell the model the rest were dropped, instead of rejecting the reply.
  - Reject `exit` while `git diff` against `start_commit` is empty. Every Verified instance needs a change, so an empty submission is always a failure.

  Not now: accepting repeated identical blocks, and native tool calling (the next step if arm 2 fails).
- **10c. Check on dev_25.** Two arm 2 runs against two fresh baseline runs, as in Step 8e. Primary: the narration measure and empty patches. Secondary: the resolved rate, not expected to move at this size. About $1 (ceiling $8). Then freeze the arm 2 config.

## Stage 11: GEPA (arm 3)

- **11a. Confirm the library.** Check that `gepa==0.1.4` on PyPI matches the source read during planning (`gepa.optimize`, the adapter interface, resume from `run_dir`, budget accounting). Install the core package only. The reflection model is called through our existing OpenAI client, so litellm is not needed. If 0.1.4 differs, or a hand-written loop would do as well, reconsider.
- **11b. The adapter** (`gepa_adapter.py` and a driver). `evaluate` runs `run_and_save` then `run_harness` and `collect_scores` per instance. A template that no longer formats is scored 0 without starting a container. Unit test with a stub agent and a stub harness.
- **Decisions already made:**
  - GEPA starts from the arm 2 prompt and optimizes once. Arm 3 is arm 2 plus the GEPA templates, so arm 3 minus arm 2 isolates GEPA.
  - It changes `system_template` and `task_template` only. Steps, limits and model stay fixed.
  - Metric: 1 if resolved, else 0. Feedback: status, F2P and P2P counts with failed test names, narration counts, a truncated patch and the last agent turns.
  - Split within the pool: val 40 (random), train 100 (half from ids the baseline failed). Train and val images stay cached during the run.
  - The GEPA val score is never reported as a result (winner's curse on 40 noisy instances). Only Stage 12 numbers are.
- **Still to decide:** the reflection model (a stronger OpenAI model than gpt-5.6-luna).
- **11c. Smoke run:** val 3, train 6, `max_metric_calls=20`, about $0.30 (ceiling $2).
- **11d. Full run:** `max_metric_calls=300`, about 20 to 25 iterations. About $3.50 in rollouts plus reflection capped by `max_reflection_cost` (suggested $10), about 10 h sequential.

## Stage 12: Three-arm comparison on the held-out 360

- **12a. Sharding (scripts done, one check left).** `shard_instances.py` splits an ids file into shards of whole (repo, version) groups, each shard runs as its own `run_batch.py` process into its own folder, and `merge_scores.py` merges the scores with checks.
  - Give each shard its own `--output-dir`. Shards started in the same second get the same run folder name (`progression.md` Step 12a).
  - **Real shard timing pilot.** Needs its own go-ahead and must not overlap another batch. It is the only check of two shards running at the same time (Docker images, disk, rate limits).
- **12b. Analysis rules (done, written early).** Fixed in `progression.md` Step 12b before any arm results: the comparisons, the outcome, the exact McNemar test and paired bootstrap interval, the second-run rule and the secondary outcomes. A change must be made before the Stage 12 runs start and recorded there with its date and reason. The analysis script is checked on Stage 8 data first.
- **12c. The runs.** Baseline, arm 2 and arm 3, one run each on all 360, plus one repeat baseline run to measure run-to-run noise on this set. Arms are interleaved or run in parallel so time-of-day and API drift affect all of them. Analysed by the rules of 12b.
- **Spend and time:** about 4 × $3.60 ≈ $14.50 (ceiling about $115), and about 4 × 360 × 124 s ≈ 50 h of machine time sequential, so less with shards. Each run needs a go-ahead.

## Spend still ahead (each needs a go-ahead)

| Step | What | Typical | Ceiling |
|---|---|---|---|
| 10c | 4 runs on dev_25 | $1 | $8 |
| 11c | GEPA smoke run | $0.30 | $2 |
| 11d | GEPA run | $3.50 + up to $10 reflection | $34 |
| 12a | shard timing pilot | under $1 | to set at the time |
| 12c | 4 runs on 360 | $14.50 | $115 |
