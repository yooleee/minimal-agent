# minimal_agent

A small coding agent for [SWE-bench](https://www.swebench.com/), written from scratch to study what actually improves its score.

The agent is a plain loop. It asks an OpenAI model for one bash command, runs it in the instance's Docker container, and repeats until the model exits. There is no agent framework. The resulting patch is scored with the official SWE-bench harness.

The project compares three versions of the agent on instances that played no part in designing them:

| Arm | What it is | Status |
|---|---|---|
| 1. Baseline | `config/default.yaml` | Built and measured |
| 2. Narration fix | `config/narration_fix.yaml`: runs the first command of a multi-command reply and refuses an exit with no change | Built and frozen, not yet measured on the comparison set |
| 3. GEPA | Arm 2 with its two prompt templates optimized by [GEPA](https://github.com/gepa-ai/gepa) | Library confirmed, adapter not built |

## Results so far

- **Baseline:** 344 of 500 SWE-bench Verified instances resolved (68.8%, 95% interval 64.6% to 72.7%), at about $0.010 and 124 seconds per instance on a Mac, using `gpt-5.6-luna`.
- **Narration fix, on 25 development instances run twice:** replies lost to format rejections fell from 17.4% of steps to 7.2%. Resolved went from 34 of 50 to 36 of 50, which is within noise. This does not estimate the fix's effect on the score.
- **Main remaining failure:** incomplete fixes. Of the 45 unresolved instances in the tuning pool, 40 fail a hidden FAIL_TO_PASS test, and no run came near the 50-step limit.

The three-arm comparison (Stage 12) has not been run. Its analysis rules were fixed in advance in `docs/progression.md`, Step 12b.

## Requirements

- Python 3.12 (3.10 or later is needed for the type annotations).
- Docker. SWE-bench images are x86_64 only, so they run under emulation on Apple Silicon.
- An OpenAI API key.
- Disk space: each instance's image is about 4 to 8 GB, with layers shared between images.

## Setup

```bash
python -m venv .venv
# macOS and Linux: source .venv/bin/activate
# Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
```

Create a `.env` file in the project root. It is gitignored.

```
OPENAI_API_KEY=your-key-here
```

Run every command from the project root.

## Usage

**These commands call the OpenAI API and cost money.** A 25-instance run costs about $0.25.

Run one instance:

```bash
python run_single.py sympy__sympy-15599
```

Run a batch, using arm 2's config:

```bash
python run_batch.py --instances-file instances/dev_25_seed1234.txt --config config/narration_fix.yaml
```

Score a finished run with the SWE-bench harness (no API calls):

```bash
python score_run.py runs/<run-folder>
```

Continue an interrupted batch. Pass the same `--config` as the original run, because a resume refuses a different one:

```bash
python run_batch.py --resume runs/<run-folder> --config config/narration_fix.yaml
```

Score each instance as it finishes and remove its image, which keeps disk use low on large runs:

```bash
python run_batch.py --instances-file <ids file> --score-each --remove-images
```

Each run gets a folder in `runs/` (gitignored) with `run.json`, `preds.jsonl`, one `<instance>.traj.json` per instance and, after scoring, `scores.json`.

## Data split

`instances/heldout_360_seed1001.txt` is the comparison set. It is not read or tuned on until Stage 12. All design and tuning use `instances/pool_140_seed1001.txt`, which contains the 25 development instances in `instances/dev_25_seed1234.txt`. Please keep to this if you extend the project, because the comparison depends on it.

## Layout

| Path | Purpose |
|---|---|
| `class_agent.py` | The agent loop, reply parsing and output truncation |
| `run_single.py` | One instance end to end |
| `run_batch.py` | Batches, with resume and per-instance scoring |
| `score_run.py` | Scores a finished run |
| `config/` | Run settings and prompts (`*.yaml`), and the Docker environment (`environments.py`) |
| `handling/` | Exceptions, run records and scoring |
| `instances/` | The instance id files |
| `sample_instances.py` | Draws seeded instance samples |
| `shard_instances.py`, `merge_scores.py` | Split a run across processes and merge the scores |
| `narration_measure.py` | Measures replies lost to format rejections in saved runs |

## Documentation

- `docs/roadmap.md`: what is left to do and the decisions already made.
- `docs/progression.md`: how each step was built, the bugs found and how each was verified.
