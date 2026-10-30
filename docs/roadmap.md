# Roadmap

The steps still to do, from the end of the Stage 9 baseline run (2026-10-30) to the Stage 12 comparison. `progression.md` records what was built and found. This file records what comes next and the decisions already made about it. When a step is done, remove it here and write it up in `progression.md`.

Working rules for every step: the code is written from skeletons with numbered TODOs (for example `TODO(12a.1)`), reviewed by running it, and every run that spends API money needs an explicit go-ahead.

## Where things stand

- **Stage 9 baseline:** 344 of 500 resolved (68.8%, 95% interval 64.6% to 72.7%). On the held-out 360 it is 249 (69.2%). Run `2026-10-29T09-34-15`, details in `progression.md` Steps 9f and 9g.
- **Data split (Step 9a):** `instances/heldout_360_seed1001.txt` is the comparison set and is not read or tuned on until Stage 12. `instances/pool_140_seed1001.txt` (which includes dev_25) is the only data that Stage 10 design and Stage 11 tuning may look at.
- **Measured costs:** about $0.010 and 124 s per instance (89 s of it agent time) on the Mac, sequential, with `--score-each --remove-images`.
- **Started early:** Step 12a (sharding) is being built now, in the main checkout. `shard_instances.py` is done (12a.1 to 12a.6). `merge_scores.py` is in progress (12a.7 and 12a.8 done).

## Stage 9: what remains

- **9h. Scoring timeout fix (before Stage 12).** A prediction whose tests time out gets no `report.json`, so `--score-each` evaluates it again after every later instance, 30 min each time (sympy-19040, Step 9f). Sharded runs hit the same problem. Likely fix: within a session, skip ids whose scoring has already failed, and leave them to `score_run.py`. Check with a stubbed harness that fails one id.
- **9i. Small fixes (optional).** In the stop message, print the last line of a Docker error, which holds the cause, instead of the first ("Unable to find image ... locally"). Make "Next command" and "Next Command" the same.
- **9j. Write the Stage 12 analysis rules into `progression.md`** before any arm results exist (the rules are below). They are fixed in advance so they cannot be adjusted after seeing results.

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

- **12a. Sharding (in progress).** Split an ids file into N shards of whole (repo, version) groups, run each shard as its own `run_batch.py` process into its own folder, then merge the scores with checks (same config, dataset, split and harness version, disjoint ids, exact coverage, every shard fully scored). Verify with toy inputs and a stubbed two-shard run. A real shard timing pilot needs its own go-ahead and must not overlap another batch.
- **12b. The runs.** Baseline, arm 2 and arm 3, one run each on all 360, plus one repeat baseline run to measure run-to-run noise on this set. Arms are interleaved or run in parallel so time-of-day and API drift affect all of them.
- **12c. Analysis rules (fixed in advance, see 9j):**
  - Primary metric: resolved rate, analysed per instance in pairs, with an exact McNemar test and a paired bootstrap interval on the difference.
  - With about 16% of instances flipping between identical runs, one run per arm detects about a 6 point difference.
  - If a difference falls between 3 and 6 points, do a second run of the arms involved. Otherwise report it as it is.
  - Secondary: empty patches, the narration measure, regressions, cost and steps.
  - Check the analysis script first on Stage 8 dev data, where the answer is known (no difference between 8d and 8e).
- **Spend and time:** about 4 × $3.60 ≈ $14.50 (ceiling about $115), and about 4 × 360 × 124 s ≈ 50 h of machine time sequential, so less with shards. Each run needs a go-ahead.

## Spend still ahead (each needs a go-ahead)

| Step | What | Typical | Ceiling |
|---|---|---|---|
| 10c | 4 runs on dev_25 | $1 | $8 |
| 11c | GEPA smoke run | $0.30 | $2 |
| 11d | GEPA run | $3.50 + up to $10 reflection | $34 |
| 12a | shard timing pilot | under $1 | to set at the time |
| 12b | 4 runs on 360 | $14.50 | $115 |
