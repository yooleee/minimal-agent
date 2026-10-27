# Progression

A record of how the harness was built, one step at a time. Each step lists what it changed, the design decisions behind it, the bugs found in review, and the checks that verified it. The code itself carries only docstrings and comments about its current behavior. The original TODO-annotated code is preserved in the first commit (`a02cc06`).

Goal of the project: one SWE-bench (bash-only) instance per evaluation, run unattended by the agent, then scored automatically.

| Stage | What | Status |
|---|---|---|
| 1 | `config/environments.py`: DockerEnvironment, LocalEnvironment, image-name helper | Done |
| 2 | Inject the environment into `Agent` | Done |
| 3 | `run_single.py`: one instance end to end, with guaranteed cleanup | Done |
| 4 | `config/default.yaml` as the single source of run settings | Done |
| 5 | Patch capture (`model_patch`) | Done |
| 6 | Run record (config, messages, result) saved to disk | Done |
| 6b | Per-call LM record, early-exit guard, prompt fixes | Done |
| 7 | Scoring with the SWE-bench harness | Done |
| 8 | Robustness and batch runs, baseline and exit-rule experiment | Done |

---

## Step 1: Environments

**Goal.** Before this step, the container name, working directory, env vars and timeout were hardcoded inside `Agent.execute_action`, and the `swe-test` container was started and removed by hand. Step 1 moved command execution behind an environment object that owns the container lifecycle.

### Design decisions

- **Contract.** `execute(command)` returns `{"output": str, "returncode": int}` with stderr merged into stdout. A nonzero exit is a result, not an exception. On a timeout it raises `subprocess.TimeoutExpired` carrying partial output. The environment never builds chat messages. Translating errors for the model is the Agent's job.
- **`Environment` Protocol.** Structural typing: any class with `start`, `execute` and `cleanup` satisfies it without inheriting. It exists so `Agent` can annotate `env: Environment` and accept either environment.
- **`__init__` stores config, `start()` does side effects.** Building the object never touches Docker, so it is cheap to construct in tests and `start()` is the one place setup can fail.
- **The container name is generated and stored before `docker run`.** `cleanup()` removes by name. If `docker run` half-succeeds (the container exists but the host call times out), there is no ID, but the name is known, so cleanup still works.
- **Platform hardcoded to `linux/amd64`.** SWE-bench images are x86_64 only, and they run under emulation on Apple Silicon. There is no platform parameter because no other value is valid.
- **The in-container `timeout`, plus a host backstop.** Killing the host-side `docker exec` client does not kill the process inside the container, which would keep running and hold CPU or locks. So each command runs as `timeout --kill-after=5 60 bash -c CMD` inside the container. The host `subprocess.run` timeout is `timeout + kill_after + host_grace` (75s), only as a backstop.
- **Exit code 124 becomes `TimeoutExpired`, and 137 does not.** GNU `timeout` exits 124 when TERM was enough. If it escalates to KILL, the exit is 137, which is also what an OOM kill or `kill -9` produces. Treating 137 as a timeout would mislabel those, so it is returned as an ordinary exit code.
- **`cleanup()` never raises.** It runs in a `finally`, and an exception there would replace the original error. It catches `TimeoutExpired` and `OSError`, prints a warning that names the container, and only resets its state after a successful removal.
- **`_docker_exec_argv()` is a separate method** so the exact argv can be checked without Docker running.
- **`LocalEnvironment` has no-op `start()` and `cleanup()`** (the Null Object pattern), so either environment drops into the same lifecycle code. Known limitation: on a host timeout, `subprocess.run` kills `bash` but not its grandchildren.
- **Image naming.** `__` in an instance id becomes `_1776_`, under `docker.io/swebench/sweb.eval.x86_64.<id>:latest`, lowercased.

### Bugs found in review

- **`"--platform linux/amd64"` as one list element.** List-form argv passes each element as exactly one argument, so Docker received a single unknown flag. A flag and its value are two elements.
- **`env` stored without copying.** The caller's dict was shared, so a later change to it would silently change the environment. Fixed with `{} if env is None else dict(env)`, which also avoids the mutable-default-argument pitfall.
- **`start()`:**
  - `shell=True` with a list ran a bare `docker` and ignored the rest.
  - `env=self.env` replaced the whole child environment, which removed `PATH` and caused a `FileNotFoundError`.
  - `-w` placed after the image was passed to `sleep` as an argument instead of to Docker.
  - Merging stderr into stdout left `stderr` empty for the error message and could contaminate the container ID.
  - `stdout=subprocess.STDOUT` caused `OSError: Bad file descriptor`, because `STDOUT` is only valid for `stderr`.
  - The timeout handler built an LM message dict, which belongs to the Agent.
- **`_docker_exec_argv()`** had no `return`, so `execute()` received `None`. `"self.cwd"` was quoted, which passed the literal text.
- **`execute()`** used `start_timeout` in the backstop (615s instead of 75s).
- **`cleanup()`:**
  - An early failure left `result` unbound (`UnboundLocalError`).
  - `OSError` was not caught.
  - Warnings did not name the container.
  - The nonzero path reset state inconsistently.

### Verification

Image pull and the manual equivalent of `start()`:

```bash
docker pull --platform linux/amd64 docker.io/swebench/sweb.eval.x86_64.sympy_1776_sympy-15599:latest
docker run -d --platform linux/amd64 --name swe-test \
  docker.io/swebench/sweb.eval.x86_64.sympy_1776_sympy-15599:latest sleep infinity
docker exec -w /testbed -e BASH_ENV=/root/.bashrc swe-test bash -c "bin/test sympy/core/tests/test_arit.py -k test_Mod"
```

Check 1, `start()` records a name and ID:

```bash
python3 -c "from config.environments import DockerEnvironment, swebench_image_name; e = DockerEnvironment(swebench_image_name('sympy__sympy-15599')); e.start(); print(repr(e.container_name), repr(e.container_id))"
docker ps -a --filter name=minimal-agent
```

Check 2, the argv has one element per argument and the env vars expanded:

```bash
python3 -c "from config.environments import DockerEnvironment as D; e = D('img', env={'PAGER': 'cat', 'BASH_ENV': '/root/.bashrc'}); e.container_id = 'abc'; print(e._docker_exec_argv('ls -la'))"
```

Check 3, `cleanup()` is idempotent:

```bash
python3 -c "from config.environments import DockerEnvironment, swebench_image_name; e = DockerEnvironment(swebench_image_name('sympy__sympy-15599')); e.start(); print(e.container_name); e.cleanup(); e.cleanup(); print(e.container_name, e.container_id)"
```

Check 4, the failure path: `start()` with a bogus image raises `RuntimeError`, and the following `cleanup()` is silent.

Check 5, `execute()` against a real container with `timeout=3`. It covers `pwd`, a nonzero exit, an in-container timeout with partial output, and no leftover process:

```bash
python3 -c "
import subprocess, time
from config.environments import DockerEnvironment, swebench_image_name
e = DockerEnvironment(swebench_image_name('sympy__sympy-15599'), timeout=3, env={'PAGER': 'cat'})
e.start()
try:
    print('pwd     ->', e.execute('pwd'))
    print('exit 3  ->', e.execute('exit 3'))
    t = time.time()
    try:
        e.execute('echo partial; sleep 100')
        print('timeout -> NOT RAISED (bug)')
    except subprocess.TimeoutExpired as x:
        print('timeout -> raised after %.1fs, output=%r' % (time.time() - t, x.output))
    print('leftover sleep 100 ->', repr(e.execute('ps -eo args | grep \"^sleep 100\" || true')['output']))
finally:
    e.cleanup()
print('cleaned up:', e.container_name)
"
```

`LocalEnvironment().execute("echo hi")` returns `{"output": "hi\n", "returncode": 0}`.

---

## Step 2: Inject the environment into the Agent

**Goal.** `Agent.execute_action` delegates to `self.env.execute(command)` instead of calling Docker itself.

### Design decisions

- **`env: Environment` is required, with no default.** A default `LocalEnvironment` would silently let the model run commands on the host if a caller forgot to pass one.
- **Injected, not constructed inside the Agent** (dependency injection). The caller owns the container lifecycle, and the Agent stays testable with `LocalEnvironment` or a stub.
- **The Agent translates timeouts.** Both timeout paths (in-container 124 and the host backstop) arrive as `TimeoutExpired`. The Agent turns that into `ExecutionTimeout` with a message for the model. It normalizes `e.output`, which can be `bytes` (from the backstop) or `None`.
- **The system-message check moved into `Agent.__init__`.** The invariant belongs to the class that relies on it, so every caller gets it.
- **The prompt's timeout was synced by hand** to the environment's 60s. Step 4 replaced this with a placeholder.

### Bugs found in review

- The instance id `sympy_sympy-15599` used a single underscore. SWE-bench ids use `__`, so the image name was wrong.

---

## Step 3: `run_single.py`

**Goal.** One command runs the agent on one instance end to end: `python3 run_single.py <instance_id>`. It replaced `get_instance.py`, `problem.txt` and the module-level code in `class_agent.py`.

### Design decisions

- **Functions split by responsibility:** `load_instance`, `build_messages`, `run_instance` and `main`. Prompt assembly can be inspected without starting Docker, and `run_instance` is importable for a later batch runner.
- **Module-level code removed from `class_agent.py`.** Importing `Agent` previously ran code that read `problem.txt`.
- **`start()` inside the `try`, `cleanup()` in the `finally`.** If `start()` fails halfway, cleanup still removes the container by name.
- **`run_instance` returns a record:** `{"instance_id", "exit_status", "model_patch"}`. `model_patch` is `None` until Stage 5. `None` means "not captured", which must stay distinct from `""` ("captured, no changes"), because scoring needs to tell them apart.
- **Seam for Stage 5.** Patch capture goes inside the `try` after `run()`, while the container still exists.
- **`load_instance` raises `ValueError` on an unknown id**, so a typo fails there with a clear message instead of as a Docker pull error. It uses `raise ... from None` to hide the irrelevant `StopIteration`.

### Bugs found in review

- `TASK_TEMPLATE` was left as `...`.
- `run_instance` returned `exit_status` instead of the record.
- `model_patch | None` raised `TypeError` (`|` is not "or" for values).
- The whole `instance` row was stored as `instance_id`.

### Verification

Running `run_instance` with a stub agent checked the orchestration without LM calls. A real run printed a record with `exit_status: Submitted`.

---

## Step 4: YAML config

**Goal.** `config/default.yaml` is the single source of run settings. The prompt's numbers are filled from the same values the code uses, so they cannot disagree.

### 4a: The YAML file

- **Top-level sections:** `dataset`, `model`, `agent`, `system_template`, `task_template`, `environment`. Model and environment are not agent settings, so they are not nested under `agent`.
- **Templates use `str.format` placeholders.** `{timeout}` comes from `environment.timeout`. `{output_limit:,}`, `{head_chars:,}` and `{tail_chars:,}` come from `agent` (`:,` prints 10000 as 10,000). A literal brace in a template would need doubling. Braces inside the values, such as code in an issue, are fine.
- **YAML `|` block rules.** The first line's indentation is stripped from every line, and extra indentation is kept as text. A `#` comment inside a block becomes part of the prompt.
- **`environment.env`** holds `BASH_ENV`, `PAGER` and `MANPAGER`.

### 4b: Agent settings as parameters

- **New keyword parameters:** `max_format_errors`, `output_limit`, `head_chars`, `tail_chars`. Their Python defaults only serve tests and quick local use. Real runs pass every value from the YAML.
- **Head/tail validation moved into `__init__`.** Before, it ran only when an output exceeded the limit (mid-run, after tokens were spent, and after the slicing it guarded). Now a bad config fails before the first LM call.
- **`run()` uses `self.max_format_errors` in both places** instead of two hardcoded `5`s, which could drift apart.
- `system_prompt` was deleted from `class_agent.py` after its text moved into the YAML.

### 4c: Wiring

- **`load_config` is called once in `main()`**, and the dict is passed down. Agent and the environments receive plain values and never know a YAML file exists.
- **`yaml.safe_load`, never `yaml.load`.** `safe_load` only builds plain data types, so a config file cannot run code. PyYAML is a direct dependency. `tomllib` (stdlib) was the alternative. pydantic validation was declined for now. Reconsider it when the config grows, is shared, or a bad key costs a run.
- **`**config["environment"]` and `**config["agent"]`.** The YAML keys match the parameter names exactly, so unpacking passes them as keyword arguments. An unexpected key fails loudly with `unexpected keyword argument` before any LM call. The model is passed separately as `model=config["model"]["name"]`.
- **The Agent is built before `env.start()`**, so a bad config fails before a container exists.
- **`--config` flag** with `type=Path` and a default of `Path(__file__).parent / "config" / "default.yaml"`, which works from any launch directory.

### Bugs found in review

- **`(instance_id, dataset_name, split) = config["dataset"]`.** Iterating a dict yields its keys, so this unpacked key names, and it overwrote `instance_id`.
- **`load_config`:**
  - `x is not dict` compares against the class object, so it was always true. `isinstance` is the type check.
  - `yaml.safe_load` was called twice. A file object keeps a read position, so the second parse started at the end of the file and returned `None`.
  - `{Path}` printed the class instead of the path.
  - The error became `TypeError` (the wrong kind of object) instead of `ValueError` (right kind, bad value), following Ruff's TRY004.
- **`config["dataset"]["dataset_name"]`.** The key is `name`.
- **`Agent(messages, **config["agent"])`** was missing the required `env`. The model was not passed, so the Python default was used silently.
- **`**config["environment"]["env"]`** (tried, not kept) would spread the env vars themselves as keyword arguments (`unexpected keyword argument 'BASH_ENV'`). `**` only works inside a call's argument list or a dict literal, which is why `env = **config` is a syntax error.
- **`Agent(...).run()` before `env.start()`.** Chaining `.run()` onto the constructor ran the whole loop before the container existed. The first command raised `RuntimeError: Container not started`.

### Verification

```bash
python3 -c "from run_single import load_config, DEFAULT_CONFIG_PATH; print(list(load_config(DEFAULT_CONFIG_PATH)))"
# ['dataset', 'model', 'agent', 'system_template', 'task_template', 'environment']

python3 run_single.py sympy__sympy-15599
# {'instance_id': 'sympy__sympy-15599', 'exit_status': 'Submitted', 'model_patch': None}

docker ps -a --filter name=minimal-agent
# no containers left behind
```

An empty config file raises `TypeError` naming the path.

---

## Step 5: Patch capture

**Goal.** After the agent finishes, the harness collects everything it changed in `/testbed` as one unified diff and stores it in the record's `model_patch`. This is the text Stage 7 will hand to the SWE-bench harness for scoring.

### Design decisions

- **The harness captures the patch itself.** It does not rely on the model printing a diff, so a formatting slip cannot lose a submission.
- **`capture_patch` builds a command string, not an argv list.** It is a caller of `env.execute()`, like the Agent. The argv list is built one layer down in `_docker_exec_argv`, where the string becomes the single argument after `bash -c`. The command also needs shell syntax (`&&`, `>`), which only works through bash.
- **`git add -A`, then `git diff --cached <base_commit>`.** Plain `git diff` skips files the agent created. Diffing against `base_commit` instead of HEAD still catches changes if the agent runs `git commit`. Checked in the image: HEAD is an extra empty "SWE-bench" commit on top of `base_commit`, and the tree starts clean. **Superseded:** this was checked on one image only. Some images add a non-empty commit, so the diff is now taken against the commit at container start (see "Step 8d fix").
- **Two calls: write the diff to `/tmp/model.patch`, then `cat` it.** `execute()` merges stderr into the output, and git writes warnings to stderr (seen in the image: `warning: LF will be replaced by CRLF ...`). Redirecting only stdout to a file keeps those warnings out of the patch. The file sits outside `/testbed` so it never appears in its own diff.
- **The output is returned without `.strip()`.** A patch must end with a newline, or `git apply` reports it as corrupt.
- **Three distinct outcomes.** A diff string, `""` (captured, no changes), and `None` (capture failed). A nonzero exit or a timeout prints a warning starting with "Patch capture failed" that includes git's own message, and returns `None`. The run's `exit_status` is kept either way.
- **It lives in `run_single.py`, typed against the `Environment` Protocol.** It is specific to SWE-bench and git, so it stays out of the generic environments. The Protocol type means it can be tested with `LocalEnvironment`.
- **The call goes inside the `try`, right after `agent.run()`.** After the `finally` the container is gone. If `run()` raises, capture is skipped. Stage 8 can decide whether a crashed run should keep its patch.
- **`base_commit` goes into the shell string unquoted.** That is safe because it is always a 40-character hex hash from the dataset. A value that could come from a user or the model would need `shlex.quote`.
- **Scratch files are kept.** `git add -A` also picks up files like `reproduce.py`. mini-swe-agent does the same, and extra files rarely affect the test run. Revisit if Stage 7 shows a failure caused by them.
- **Known limitations.** There is no `--binary` flag, so a changed binary file cannot be applied. `execute()` decodes with `errors="replace"`, so non-UTF-8 content is mangled.

### Bugs found in review

- **The failure warnings left out git's message**, which is the only explanation of why capture failed in an unattended run.
- **The second warning printed the first call's output** (copy-paste), so a failed `cat` would have shown an empty reason.
- **Nested same-type quotes in f-strings** (`f"{x["key"]}"`) only parse on Python 3.12+ (PEP 701). On older Pythons the whole file fails to import. Single quotes inside the braces work everywhere.

### Verification

Run by hand first, in a manually started container:

```bash
docker run -d --platform linux/amd64 --name swe-test docker.io/swebench/sweb.eval.x86_64.sympy_1776_sympy-15599:latest sleep infinity
docker exec -w /testbed swe-test bash -c "echo x >> README.rst && echo new > zz_new.py"
docker exec -w /testbed swe-test bash -c "git add -A && git diff --cached 5e17a90c19f7eecfa10c1ab872648ae7e2131323 > /tmp/model.patch"; echo "exit: $?"
docker exec -w /testbed swe-test bash -c "cat /tmp/model.patch"
docker rm -f swe-test
```

Checks 1 to 3 used `LocalEnvironment` on a temporary git repo:

- An edit, a new file and a committed change all appear in the patch. The patch ends with `"\n"` and passes `git apply --check` on a fresh clone at the base commit.
- A clean repo returns `""`, not `None`.
- A bogus `base_commit` returns `None` with the warning `128 Patch capture failed due to fatal: bad object ...`. A timeout also returns `None`.

Check 4, end to end. `run_instance` was run against Docker with a scripted LM (`Agent.query_lm` replaced, so there were no API calls) that appended one line to `sympy/core/mod.py` and then exited. The record had `exit_status: Submitted` and a `model_patch` starting with `diff --git a/sympy/core/mod.py`, and no container was left behind. A real run returned `model_patch: ''`. Capture worked, but the agent left no changes, so that run would score as unresolved.

---

## Step 6: Run records

**Goal.** Every run leaves a folder on disk that answers what the agent did, with which settings, on which version of the harness. It also writes the predictions file that Stage 7 scores.

```
runs/                                    gitignored
  2026-10-19T23-11-14/                   one folder per invocation (UTC timestamp)
    run.json                             config, harness commit, Python version, argv
    sympy__sympy-15599.traj.json         one trajectory per instance
    preds.jsonl                          one prediction line per instance
```

### Design decisions

- **The save functions live in `handling/records.py`, not `run_single.py`.** `run_single.py` may be replaced once batch runs exist (Stage 8), and the batch runner needs the same functions with one shared `run_dir`.
- **`run_instance` returns data, and `main()` writes the files.** `run_instance` stays testable without touching the disk.
- **JSON and JSONL from the standard library.** Messages are arbitrary text, and JSON escapes it safely. Revisit YAML only if a concrete need appears.
  - `run.json` and the trajectories use `indent=2` for reading by eye.
  - `preds.jsonl` has one record per line with no indent. A batch that crashes halfway keeps every line already written.
  - `ensure_ascii=False` keeps non-ASCII text readable, and `encoding="utf-8"` makes that safe on Windows.
- **`json.dump` vs `json.dumps`.** `dump` writes straight into an open file and suits a file holding one document. `dumps` returns a string, which `append_prediction` needs so it can add the newline itself. Calling a json function on that string again would double-encode it into one quoted string.
- **Nothing is silently overwritten.** `make_run_dir` uses `mkdir(exist_ok=False)`, and the trajectory and `run.json` open with mode `"x"`. The shared `runs/` parent uses `exist_ok=True`, and `preds.jsonl` uses append mode `"a"` on purpose.
- **Folder names use UTC with hyphens in the time** (`%Y-%m-%dT%H-%M-%S`). A colon is not allowed in Windows file names, and the format sorts alphabetically in time order.
- **`run.json` is written before `run_instance`**, so a crashed run still records its config and harness version. It saves the config values, not the YAML path, because the file can change later.
- **The harness version is `{"commit", "dirty"}`.** A shared helper `_run_git` returns `None` when git fails or cannot run (nonzero exit, `OSError`, `TimeoutExpired`). That keeps three states apart: a clean tree (`False`), uncommitted edits (`True`), and "could not check" (`None`). A missing version stamp never stops a run.
- **Two clocks.** `started_at` and `finished_at` are ISO 8601 UTC strings (`2026-10-19T23:05:54.029802+00:00`, biggest unit first) that record when the run happened. `duration_s` is the difference between two `time.monotonic()` readings, because the wall clock can jump (time sync, sleep) and a monotonic clock only moves forward.
- **`preds.jsonl` keeps `model_patch: null` when capture failed.** It is not turned into `""`, which would erase "failed" vs. "no changes". How the SWE-bench harness treats `null` is checked in Stage 7.
- **`main()` prints a one-line summary**, not the full record (which now holds every message). A failed capture prints `Capture failed` instead of a character count.
- **Known gap.** If `agent.run()` raises, `run_instance` returns nothing, so only `run.json` is written. Recording an errored run is Stage 8 work. The save functions take a plain dict, so an error record fits the same seam.
- **Deferred.** Token usage and cost need `query_lm` to read `response.usage`.

### Bugs found in review

- **`_harness_git_state`:**
  - No `try/except`, so a missing git raised `FileNotFoundError` and would have stopped the run. "Don't raise" means catch and convert, not leave the exception handling out.
  - `dirty` was decided by empty stdout. A clean tree became `None` ("could not check"), and a dirty tree stored git's whole output instead of `True`. The fix decides by returncode first, then by output.
  - The returned keys (`Commit`, `messy_status`) did not match the docstring (`commit`, `dirty`).
- **`append_prediction`:**
  - The leftover `raise NotImplementedError` ran after the line was written.
  - `"model_name_or_patch"` was a typo of the harness key `"model_name_or_path"`. It would only have failed at scoring time.
- **The summary print:**
  - `"..." if None else len(...)` tested the literal `None`, which is always false, so a failed capture crashed on `len(None)`.
  - The next version, `len(x) is None`, called `len` before the test, and `len` never returns `None`.
  - A `result['nstance_id']` typo raised `KeyError` on every run.
- **Docstrings:** the `run_instance` docstring briefly contained a pasted copy of the `return` dict, and listed the timestamps as floats.

### Verification

`make_run_dir`, `save_run_metadata`, `save_trajectory` and `append_prediction` were checked in a scratch folder:

- Two calls in the same second raise `FileExistsError`, and a second apart they create two folders.
- `run.json` has all five keys and keeps non-ASCII text as real characters.
- A trajectory reads back equal to the saved dict, and saving the same instance twice raises.
- `preds.jsonl` gets one parseable line per call, with `None`, a diff, and `""` kept distinct.

`_harness_git_state` was run in four situations:

- **This repo:** a commit and `dirty: True`.
- **A clean repo:** `dirty: False`.
- **Outside a repo:** `None` for both.
- **Without git on PATH:** `None` for both, with no exception.

The system Python on macOS is 3.9, and the project's `X | None` annotations need 3.10 or newer.

End to end, `main()` ran against Docker with a scripted LM: one edit, then `exit`, and no API calls.

- **A normal run** produced all three files, a trajectory with nine keys and 6 messages, and a summary with `patch_chars = 298`.
- **With `capture_patch` forced to `None`**, the summary printed `Capture failed` and `preds.jsonl` held `"model_patch": null`.
- **No container was left behind** in either case.

### Finding from the first real run

Run `2026-10-19T23-11-14` on `sympy__sympy-15599` had `exit_status: Submitted`, `step_count: 1`, `duration_s: 11.99` and `model_patch: ''`. No command ever reached the container.

The single assistant reply narrated a full session (inspecting `Mod.doit`, "the source edit has been applied", "the verification command completed without surfacing any error") and ended with its only bash-action block, `exit`. The system prompt already forbids describing results before seeing them. This is most likely the cause of the earlier real run that also returned `''`.

**Hypothesis, not yet verified.** The reply has exactly one sentence per line. `query_lm` joins the text of every `message` item in `response.output` with `"\n"`. That fits the model emitting several separate short messages in one response, which then read as a whole session. The trajectory cannot confirm it, because the join discards the item boundaries.

**Options, in the recommended order:**

1. Record the response's output item types (and later token usage) in the trajectory, so the hypothesis can be checked.
2. Reject an `exit` when no command has run yet.
3. Name the failure in the system prompt.

`harness.dirty` was `True` for this run because Stage 6 was not committed yet, so the commit alone cannot reproduce it.

## Step 6b: What the API returned, and the early exits

**Goal.** Test the Step 6 hypothesis, then stop runs from ending before any command has run.

### Design decisions

- **`Agent.lm_calls` holds one entry per API call**, saved in the trajectory next to `messages`. Each entry has `step`, `output_types`, `message_items` and four token counts. It is appended before `parse_action`, so a rejected reply is still recorded. That call happened and was billed.
- **`message_items` keeps each message's `phase` and text together** (`{"phase", "text"}`). The OpenAI SDK marks each message item as `"commentary"` or `"final_answer"`. An earlier version stored phases in their own list, next to `output_types`. Two parallel lists only stay correct while their order matches, so the phases moved into the items.
- **One loop builds both the record and the returned text.** Each message's parts are joined first, then the messages are joined with `"\n"`. The model receives the same string as before.
- **No separate message count.** It equals `output_types.count("message")`.
- **Token counts are `None` when `response.usage` is `None`.** "Not reported" stays different from 0. `reasoning_tokens` (hidden reasoning, billed as output) and `cached_tokens` (prompt-cache hits, billed cheaper) are included for later cost work.
- **`PrematureExit` rejects `exit` while `commands_run == 0`.** It is a new `NonTerminatingException`, not a `FormatError`, because the reply was well formed and should not use up the format-error budget. `commands_run` is incremented before `execute_action`, so a command that times out still counts. Each rejection costs a step, so `StepLimit` bounds a model that keeps trying to exit.
- **Prompt changes.** The model is told that it cannot run commands itself, that only the one block in its reply runs, and that `exit` must follow command output that verifies the fix. The line asking it to describe "what you will do next" was removed, because it invited narration.
- **The format-error message** now separates its sentences with newlines. Implicit string concatenation had joined them with no space.

### Bugs found in review

- `"\n".join(message_items)` joined dicts, which raises `TypeError` on the first API call. The next version, `("\n".join(items) for items in message_items["text"])`, indexed a list with a string and returned a generator instead of a string. The fix joins `item["text"]` for each item.
- The new prompt line was added next to the old narration line instead of replacing it, so the prompt asked for both.
- The unused `from asyncio.subprocess import STDOUT` import was first only moved by an import sorter, and the edit that removed it was not saved.

### Verification

A fake API client, running real `run()` calls with no API spend, confirmed:

- A reply with a block in both a commentary and a final-answer message is recorded with each block under its own phase, and is rejected as a format error.
- `usage=None` records `None` for all four token counts.
- A first reply of `exit` is rejected with `PrematureExit`, and the run continues.

### Findings from real runs

All runs are on `sympy__sympy-15599`.

- **The hypothesis is confirmed.** Before the fix, four runs (`2026-10-20T00-44-28` to `00-48-35`) each ended after one API call, with an empty patch. Each call returned 6 to 8 messages, interleaved with reasoning items. Read together, they narrated a whole session that never ran, then ended with `exit`. A later run with `message_items` showed the phases: many `commentary` messages, then one `final_answer`.
- **The prompt change did not stop the first-call narration.** It still happened in every run afterwards. What recovered those runs was the guard and the format check:
  - In five runs (`2026-10-20T03-30-07` to `03-44-27`), the first reply ended in `exit` three times, which the guard caught. The other two times it held several blocks, which the format check caught.
  - After that one correction, most calls held a single message.
  - All five ended `Submitted` with a non-empty patch. So did `2026-10-21T00-57-20`.
- **Format errors.**
  - In `00-57-20`, one step put its only block in a commentary message and left the final answer empty. Parsing only `final_answer` would have rejected a valid reply, so that option is ruled out.
  - The one format error in that run was the same 785-character block repeated in both phases.
  - Accepting a reply whose blocks are all identical is a possible fix. It is not made yet, because it rests on one example.
- **The existing tests matter.**
  - `pytest` is not installed in the image. In two runs the model ran the existing tests anyway: it imported `test_arit.py` directly, or used `bin/test`.
  - Both runs found that a fix like the issue's suggestion broke `test_Mod` on a float case, then iterated: 29 steps and 242 s, and 15 steps and 100 s.
  - Three of the earlier runs submitted after 5 to 11 steps without running `test_Mod`. Their patches may contain that regression. Stage 7 scoring will show it.

## Step 7: Scoring with the SWE-bench harness

**Goal.** Score a finished run folder with the official SWE-bench harness, and keep one status and the test results per instance next to the run.

```
runs/2026-10-21T00-57-20/
  gpt-5.6-luna.2026-10-21T00-57-20.json  harness summary (all instances)
  logs/run_evaluation/<run_id>/<model>/<instance_id>/
    report.json                          per-instance test results
    test_output.txt                      raw test log
  scores.json                            our summary, written by score_run.py
```

Usage: `python score_run.py runs/<run_id>`. Runs made before the dataset switch also need `--dataset-name SWE-bench/SWE-bench_Verified`.

### Design decisions

- **swebench 5.0.2, pinned in `requirements.txt`.** The scorer's version decides the result, so it is pinned and recorded in every `scores.json`.
- **The dataset moved from `princeton-nlp/SWE-bench_Verified` to `SWE-bench/SWE-bench_Verified`.** swebench 5.x reads `image`, `eval_script` and `log_parser` from each instance, and only the second has them. The two agree on every shared field except `PASS_TO_PASS` in 2 instances.
- **Scoring is its own command.** `score_run.py` sits next to `run_single.py`. Scoring takes minutes of Docker time, needs no API calls, and can be rerun on an old run folder without rerunning the agent.
- **The harness is called through its command line**, not its Python functions. The CLI is its public interface, while its internals changed between major versions. `sys.executable` runs it in the same Python environment without a hard-coded venv path.
- **The harness runs with `cwd=run_dir`** and the run folder's name as its run id. Its summary and `logs/` land inside the run folder, so everything about one run stays in one place. `--instance_ids` limits it to the run's own instances. Without it, every other dataset instance is listed as "incomplete".
- **Five statuses:** `resolved`, `unresolved`, `empty_patch`, `capture_failed` and `error`.
  - The harness counts `model_patch: null` and `""` both as an empty patch. `collect_scores` splits them again using `preds.jsonl`, so a failed capture stays different from "the agent changed nothing".
  - `error` (the harness could not evaluate) stays different from `unresolved` (the tests ran and failed).
  - The harness's own "incomplete" means "in the dataset but not predicted", so it never applies here.
  - An instance in none of the harness's id lists raises `ValueError`, because that means the report format was misread.
- **Test results are `None` when no tests ran** (no `report.json`, or one that cannot be read). That is different from empty lists. Each instance keeps `fail_to_pass` and `pass_to_pass` as `{"success": [...], "failure": [...]}`, so an unresolved instance can be diagnosed from `scores.json` alone.
- **`safe_model_name`** holds the harness rule that turns `/` in a model name into `__`. The summary file name and the log folder both depend on it, so it lives in one place.
- **`--dataset-name` and `--split` override `run.json`.** By default a run is scored against the data the agent saw. The override exists for runs made before the dataset switch, and the dataset actually used is recorded in `scores.json`.
- **`scores.json` opens with mode `"x"`**, following the Step 6 rule that nothing is silently overwritten. To rescore, delete `scores.json` and `logs/` first. The harness skips any instance that already has a `report.json` for that run id.
- **`read_predictions` raises `ValueError` for an empty `preds.jsonl`.** A missing file (`FileNotFoundError`) and a file with no predictions are different states, and the empty case would otherwise surface as a bare `StopIteration`.
- **Known limitation.** The harness also labels some `unresolved` and `error` instances as likely infrastructure failures or ambiguous failures (`infra_failure_ids`, `ambiguous_failure_ids`, `failure_reasons`). These are extra labels, not extra statuses. `scores.json` does not record them yet, so an instance that failed because of Docker looks the same as one whose patch was wrong. Batch runs (Stage 8) will need this, and it fits as one more per-instance field.

### Bugs found in review

- **`read_predictions`** parsed the whole file with `json.loads(f.read())` instead of one line at a time. The first empty-file check used `predictions is Empty`, where `Empty` was `queue.Empty`, imported automatically by the editor. `{} is Empty` is always false, so the check never fired.
- **`run_harness`** looked up the model name with `predictions["instance_id"]`, which is not a key of the dict. A relative `--report_dir` combined with the changed `cwd` would have nested the folder.
- **`collect_scores`:**
  - It wrote statuses into the harness summary and into the prediction dicts instead of a local `instances`. A typo (`captured_failed`) raised `KeyError` in the counts.
  - The first version of the test results read `report.json` without checking that it exists, skipped the `instance_id` level and misspelled `tests_status`.
  - Lowercase keys were used to read the harness file, whose keys are uppercase. The broad `except KeyError` caught that and recorded a resolved run as "no tests ran", with no error.
  - Trailing commas turned both test results into one-item tuples.
- **`save_scores`** was copied from `save_trajectory` and built the path from `scores["instance_id"]`, which does not exist in a run-level dict.
- **Renaming the helper** changed the definition but not its two call sites, which raised `NameError`.
- **`load_run_dataset`** passed `encoding` to `json.load` instead of `open`, and skipped the `config` level of `run.json`.
- **`main`** parsed `--dataset-name` but never used it. The first per-instance print took `len()` of the test dicts (always 2 keys), which printed `0.5` for an instance that passed every test.

### Verification

`collect_scores` was run on copies of a real scored run:

- The real run gives `resolved`, F2P 1/1 and P2P 80/80.
- `null` and `""` patches give `capture_failed` and `empty_patch`, with `None` test results.
- An empty `report.json` keeps the harness status and gives `None` test results.
- An instance missing from every id list raises `ValueError`.
- A missing `preds.jsonl` raises `FileNotFoundError`, and an empty one raises `ValueError`.

End to end, `score_run.py runs/2026-10-21T00-57-20 --dataset-name SWE-bench/SWE-bench_Verified` ran the harness in Docker (about 25 s for one instance):

- The printed line was `sympy__sympy-15599 status = resolved F2P 1/1, P2P 80/80`.
- The summary, `logs/` and `scores.json` were all inside the run folder, with nothing in the project root and no container left behind.
- Running the same command again raised `FileExistsError` and left `scores.json` unchanged.

### Findings from scoring

All 13 earlier runs on `sympy__sympy-15599` were scored with `--dataset-name SWE-bench/SWE-bench_Verified`, and none raised an error.

| Result | Runs |
|---|---|
| resolved (F2P 1/1, P2P 80/80) | `00-48-59`, `03-30-07`, `03-44-27`, `00-57-20` |
| unresolved (F2P 0/1, P2P 80/80) | `23-14-19`, `03-40-30`, `03-41-17`, `03-41-54` |
| empty_patch | `23-11-14`, `00-44-28`, `00-46-55`, `00-48-03`, `00-48-35` |

- **Running the existing tests decided the outcome.** All four resolved runs ran `test_Mod` or the whole of `test_arit.py` before `exit` (12 to 29 steps). None of the four unresolved runs did (5 to 11 steps). This comes from searching the assistant messages for the test names, not from reading every trajectory.
- **Three unresolved runs submitted the same patch** (`23-14-19`, `03-40-30` and `03-41-17`, identical hashes). It is most likely the fix suggested in the issue, which Step 6b found fails on a float case.
- **The failure is the FAIL_TO_PASS test itself.** All 80 PASS_TO_PASS tests passed in every run, so the unresolved patches broke nothing else. They did not fully fix the bug.
- **The five empty patches are the early-exit runs from Step 6b**, from before the `PrematureExit` guard.
- **Next lever (Stage 8).** The exit rule asks for output that verifies the fix, and the model often meets it with its own small check. Pointing the rule at the repository's own tests is the most promising change. This rests on one instance and should be checked on more before it is treated as general.

## Step 8a: Recording a run that raises

**Goal.** Close the Step 6 known gap. Before this step, an exception from `agent.run()` (an API error, for example) left only `run.json`: the messages and LM calls already paid for were lost, and in a batch the error would also stop every instance after it. Stage 8 is split into 8a (this step), 8b (batch runner), 8c (failure labels in `scores.json`), 8d (baseline batch), 8e (exit-rule experiment) and 8f (this record).

### Design decisions

- **`run_instance` catches `Exception` around `env.start()`, `agent.run()` and patch capture.** The exceptions in `handling/exceptions.py` never reach it, because `Agent.run()` already catches them all. What does reach it comes from outside the harness: OpenAI errors, a failed image pull, or a bug not found yet. The catch is broad on purpose (fault isolation: one instance failing must not stop a batch). It stops at `Exception`, so Ctrl-C (`KeyboardInterrupt`) still ends the run.
- **Errors before the `try` still raise.** An unknown instance id or a bad config in `Agent.__init__` would hit every instance in a batch, so failing fast beats recording the same error many times.
- **The record gets `exit_status: "Error"` and an `error` field**, `{"type", "message", "traceback"}`, or `None` for a normal run. `type` is `type(e).__name__`, the same idiom `Agent.run` uses for its exit reasons, so it is a JSON string.
- **A local `started` flag decides whether to capture a patch after a crash.** The `except` runs before the `finally`, so the container still exists if `start()` had returned. If `start()` itself raised, `capture_patch` would fail with "Container not started", so it is skipped. The flag is local because the `Environment` Protocol has no `container_id`.
- **The partial patch stays in the trajectory, and the prediction gets `model_patch: null`.** A crashed run is never scored as a submission. The rule lives in `append_prediction`, which owns the predictions format.
- **`preds.jsonl` gains a fourth key, `agent_error`**, holding the error type or `None`. It is written on every line, so `None` means "did not crash" rather than "older file". Older `preds.jsonl` files lack it, so scoring will read it with `.get()`. swebench 5.0.2 only requires `instance_id` in a prediction (`utils.get_predictions_from_file`), so the extra key is ignored, and `null` is scored as an empty patch. Without the marker, a crash would look like `capture_failed`. Leaving the instance out of `preds.jsonl` would drop it from the denominator.
- **The summary line shows the error type instead of `patch_chars` on an errored run.** On a failed start, `model_patch` is `None` because capture never ran, and the old label would have said "Capture failed". Nothing in the project parses this line, so its contents can change freely.

### Bugs found in review

- **`"type": Exception`** stored the base class itself instead of the raised type, so every error read as `Exception`. A class object is also not JSON, so `save_trajectory` raised `TypeError` partway through writing and left a truncated trajectory that its mode `"x"` then blocked from being rewritten.
- **The summary print:**
  - The first version printed the error line and then the normal line, so an errored run printed twice, with the whole `error` dict including the traceback.
  - `result["error"]["type"] is not None` as the condition indexed `None` on a normal run (`'NoneType' object is not subscriptable`). The check belongs on `result["error"]`.
  - `... if ... else None` inside `print` still passed `None` as an argument, which printed. The fix builds a `parts` list, appends conditionally and calls `print(*parts)`.
- **`append_prediction`:** the normal branch first lacked `agent_error`, and the two branches repeated the whole dict. The fix sets `model_patch` and `agent_error` in the `if/else` and builds the dict once.

### Verification

`main()` ran against Docker with `Agent.run` replaced by a stub (no API calls), in four cases:

| Case | Summary line ends with | `preds.jsonl` (`agent_error`, `model_patch`) |
|---|---|---|
| `run()` edits a file, then raises `ConnectionError` | `Error type: ConnectionError` | `ConnectionError`, `null` |
| `start()` raises `RuntimeError` | `Error type: RuntimeError` | `RuntimeError`, `null` |
| Normal run with one edit | `patch_chars: 384` | `None`, 384 characters |
| Normal run with `capture_patch` forced to `None` | `patch_chars: Capture failed` | `None`, `null` |

- In the crash case, the trajectory kept the partial patch and the error's type, message and traceback. In the failed-start case it had no patch.
- swebench's `get_predictions_from_file` loaded the crashed run's line with its extra key.
- No container was left behind in any case.

## Step 8b: Batch runner

**Goal.** Run several instances, one after another, into one run folder that `score_run.py` can score as a whole: `python run_batch.py <id> [<id> ...]` or `python run_batch.py --instances-file <path>`.

```
runs/2026-10-25T10-07-18/
  run.json                           config, harness version, and the planned instance_ids
  <instance_id>.traj.json            one per instance, written as each one finishes
  preds.jsonl                        one line per finished instance
```

### Design decisions

- **`run_and_save(run_dir, instance_id, config)` in `run_single.py`** holds the per-instance steps: run, save the trajectory, append the prediction, print the summary line, return the record. Both `run_single.main()` and `run_batch.main()` call it, so the two entry points cannot drift apart. Saving inside the helper means a batch that stops halfway keeps every instance already finished.
- **`run_batch.py` imports from `run_single`** (`run_and_save`, `load_config` and the default paths) instead of a new shared module. `main()` is guarded, so the import has no side effects. If `run_single.py` is ever retired, these move to a shared module.
- **`run.json` records `instance_ids`**, the ids the run planned to attempt, in order. `preds.jsonl` only lists instances that finished, so without it a batch stopped by Ctrl-C could not be told apart from one that never requested the missing ids. Single runs write `[id]`, so every `run.json` has the same keys.
- **`validate_instance_ids` runs before `make_run_dir`**, so a bad list leaves no empty folder and costs nothing. It rejects:
  - an empty list.
  - duplicates, because `save_trajectory` opens with mode `"x"` and a second run of the same id would be paid for, then fail outside `run_instance`'s catch and stop the batch.
  - ids missing from the configured dataset and split. The dataset is loaded once and its `instance_id` column turned into a set, so each lookup is direct. In `datasets` 5.x that column is a `Column` object, not a list, but it is iterable.

  Each error lists every bad id at once. Cheap checks come first, so an empty or duplicated list never loads the dataset.
- **`read_instances_file` keeps file order and does not remove duplicates.** A mistake in the file is reported by validation instead of being silently fixed.
- **Exactly one id source.** Positional ids and `--instances-file` are both optional to argparse. `main()` checks `bool(ids) == bool(file)`, which is true when both or neither are given, and calls `parser.error`, which prints the usage and exits with code 2.
- **Progress and tally.** `enumerate(..., start=1)` gives the `[2/8]` count, printed before each instance so the slow part shows which one is running. A `collections.Counter` tallies each record's `exit_status` (a missing key counts as 0), printed at the end as `Submitted 2, Error 1` with the `score_run.py` command to run next.
- **Sequential on purpose.** Under amd64 emulation on Apple Silicon, parallel containers mostly compete for CPU. Revisit with an x86 host or batches of more than about 50.
- **Deferred:** `--resume` (skip ids already in `preds.jsonl`) and removing each image after its run. Each SWE-bench image is about 4 GB, so the second becomes necessary past roughly 10 to 20 instances.
- **Accepted cost.** `run_instance` still calls `load_instance` per instance, which reads the dataset from the local Hugging Face cache each time. If that becomes slow, `run_instance` can take the dataset row instead.

### Bugs found in review

- **`save_run_metadata`:**
  - The first call passed `args.instance_id`, a string, where a list was expected. Python does not check the hint, so `run.json` would have stored a bare id, and code looping over it would see characters.
  - With the argument order changed in the definition but the edited caller not yet saved, `run.json` stored the config under `instance_ids` and the id list under `config`, which `score_run.load_run_dataset` could not read. The saved call matches the definition.
- **`run_single.main()`** briefly created the folder and `run.json` without calling `run_and_save`, so nothing ran.
- **`read_instances_file`:** `line.strip()` on its own line discarded the result (strings are immutable), and `if line is not None` was always true (a blank line is `"\n"`, not `None`). Both ids and blank lines kept their newline.
- **`run_batch.main()`:**
  - `args.instance_file` for the attribute argparse names `instances_file`, and `args.run_dir` for `output_dir`.
  - `bool(a) == bool(b) is True` is a chained comparison, `(a == b) and (b is True)`, so "neither source" slipped through.
  - The ids always came from the file, and then the file branch assigned the `Path` itself instead of reading it (`TypeError: 'PosixPath' object is not iterable`).
  - `config["dataset"]["items"]` for the key `name`.
  - `print("Tally:", {status_string})` built a set, because braces outside an f-string are a set literal.

### Verification

Argument and validation cases, with no run folder created in any of them:

| Input | Result |
|---|---|
| ids and `--instances-file` | argparse error |
| neither | argparse error |
| `sympy__sympy-1559` (typo) | `ValueError` naming it, with dataset and split |
| two typos around a real id | `ValueError` naming exactly the two typos, in input order |
| `[]` / `['a','b','a']` | empty-list error / `Duplicates found ... ['a']` |
| a file with blank lines and padded ids | ids read in order, stripped, then validated |

End to end, `run_batch.py --instances-file` ran three real Verified ids against Docker with `Agent.run` replaced by a stub that edits a file and raises `ConnectionError` on the second instance (no API calls). All three ran on the local sympy image to avoid pulling about 8 GB, so capture failed for the other two, whose base commits are not in that repo.

- Progress lines `[1/3]` to `[3/3]` printed, and the crash on the second instance did not stop the third.
- One run folder held three trajectories, three `preds.jsonl` lines (the second with `agent_error: ConnectionError` and `model_patch: null`) and `run.json` with all three ids in order.
- The last line was `Tally: Submitted 2, Error 1` with the `score_run.py` command.
- No container was left behind.

`run_single.py` still writes all three files for a normal and a crashed stub run, its `run.json` holds `instance_ids: [id]`, and `score_run.load_run_dataset` reads it.

## Step 8c: Failure labels and the agent_error status

**Goal.** Close the Step 7 known limitation, so `scores.json` tells an instance that failed because of the environment apart from one whose patch was wrong. Also give the crashed runs from Step 8a their own status instead of letting them read as failed captures.

### Design decisions

- **`agent_error` is a sixth status.** The harness counts a `null` patch as an empty patch, and a crashed run's prediction has `model_patch: null`. `collect_scores` already split `capture_failed` from `empty_patch` using `preds.jsonl`. It now checks `agent_error` first in that branch, because a crash also has a `null` patch and would otherwise read as `capture_failed`. The order is `agent_error`, then `capture_failed`, then `empty_patch`. The summary counts are built from `STATUSES`, so they include the new status without other changes.
- **`.get("agent_error")` for older files.** `preds.jsonl` from before Step 8a has no such key, and `.get()` returns `None`, which correctly means "no agent error".
- **The harness's failure labels are extra fields, not statuses.** swebench 5.0.2 (`harness/infra_failure.py`, used from `reporting.py`) reads the logs of every `unresolved` and `error` instance after scoring and matches them against fixed patterns, first match winning. Each pattern has a reason code and a tier. Environment patterns (`out_of_memory`, `container_unavailable`, `network_unreachable` and others) put the id in `infra_failure_ids`. Patterns a bad patch could also cause (`missing_module`) put it in `ambiguous_failure_ids`. The reason code goes in `failure_reasons`. The ids stay in `unresolved_ids` or `error_ids`, so the denominator is unchanged. Each instance in `scores.json` gains:
  - `failure_label`: `"infra"`, `"ambiguous"` or `None`.
  - `failure_reason`: the reason code or `None`. The label says how far to trust the reason, and the reason says what to fix.
- **`None` has two meanings, and `status` tells them apart.** For `unresolved` and `error` it means the harness checked and found no environment pattern. For every other status the harness never checks, so the label does not apply. A third value was not added because the meaning follows from `status`.
- **`[]` for the label lists, `.get()` for the reason.** `infra_failure_ids`, `ambiguous_failure_ids` and `failure_reasons` are always present in the pinned 5.0.2 report, so a missing key means the format changed and should fail loudly. `failure_reasons` only holds classified ids, so a missing id there is normal.
- **`score_run.py` builds each line from a parts list**, the same pattern as `run_single.run_and_save`. The label can appear with or without test results (`unresolved` with `infra`, or `error` with no `report.json`), so adding it to two separate print calls would have repeated the condition. The parts are separated by spaces only. Trailing commas on parts had left a comma at the end of some lines and none between others.

### Bugs found in review

- `predictions.get("agent_error")` read from the dict of all predictions, whose keys are instance ids, so it was always `None`. The loop variable is `prediction`.
- `if not prediction.get("agent_error")` inverted the test. Every normal empty patch became `agent_error` and every crash became `capture_failed`. The check is `is not None`.
- `summary["summary_reasons"]` for the key `failure_reasons` raised `KeyError` on every instance, including resolved ones.
- `... if not None else None` was a conditional that always took its first branch, because `not None` is `True`.
- The first print version showed the reason without the label, so an ambiguous `missing_module` read the same as an infra failure.

### Verification

`collect_scores` ran on copies of the scored run `2026-10-21T00-57-20`, with its harness summary and `preds.jsonl` edited per case. None of our real runs has an environment failure, so the labels were set by hand.

| Case | Status | Label, reason |
|---|---|---|
| The real resolved run | `resolved` | `None`, `None` |
| Unresolved, not classified | `unresolved` | `None`, `None` |
| Unresolved, in `infra_failure_ids` | `unresolved` | `infra`, `out_of_memory` |
| Unresolved, in `ambiguous_failure_ids` | `unresolved` | `ambiguous`, `missing_module` |
| `null` patch with `agent_error: "ConnectionError"` | `agent_error` | `None`, `None` |
| `null` patch with `agent_error: None` | `capture_failed` | `None`, `None` |
| `null` patch, old line with no `agent_error` key | `capture_failed` | `None`, `None` |
| `""` patch | `empty_patch` | `None`, `None` |

`score_run.main()` ran on three of these copies with the harness call replaced by the edited summary:

```
Instance_id: sympy__sympy-15599 status: resolved F2P: 1/1 P2P: 80/80
Instance_id: sympy__sympy-15599 status: unresolved F2P: 1/1 P2P: 80/80 Failure_reason: out_of_memory Failure label: infra
Instance_id: sympy__sympy-15599 status: error Failure_reason: missing_module Failure label: ambiguous
```

The last case also had its `report.json` removed, so it shows the label on a line without test results.

## Step 8d: Baseline batch on a dev set

**Goal.** Measure the current harness on more than one instance, as the baseline that the Step 8e exit-rule variant is compared against. This section records the dev set and the decisions behind it. The results are in "Step 8d results", after the capture fix that the first batch led to.

### The dev set

`instances/dev_25_seed1234.txt` holds 25 SWE-bench Verified ids, drawn with seed 1234 and excluding `sympy__sympy-15599`, which already has 13 runs.

| Repo owner | astropy | django | pydata | pytest-dev | scikit-learn | sphinx-doc | sympy |
|---|---|---|---|---|---|---|---|
| Instances | 4 | 8 | 2 | 2 | 1 | 4 | 4 |

Verified has 500 instances across 12 repos (django 231, sympy 75, sphinx 44, matplotlib 34, scikit-learn 32, astropy 22, xarray 22, pytest 19, pylint 10, requests 8, seaborn 2, flask 1). The draw has fewer django instances than that (8 of 25 against 46%), which is normal sampling noise at this size. It was kept as drawn, because redrawing until the mix looks right would turn a random sample into a chosen one.

### Design decisions

- **Size: 25 instances (5% of Verified), on gpt-5.6-luna.** The size was chosen from cost, time and precision:
  - Cost was estimated from the 7 working sympy runs, using gpt-5.6-luna's prices per 1M tokens ($0.20 input, $0.02 cached input, $1.20 output). About 87% of their input was billed at the cached rate. `query_lm` uses the automatic prompt cache, so explicit cache-write prices were taken not to apply. Output includes reasoning tokens.
  - That gives a mean of about $0.013 per run, so about $0.32 for 25 instances. Every step resends the whole history, so input grows faster than the step count. A run at the 50-step limit is estimated at about $0.08 (the 29-step run's input scaled by (50/29)²), which puts the ceiling for 25 instances at about $2.05. Step 8e repeats the run, so the total is about double.
  - All 7 runs are on one sympy instance, so the typical figure is the least certain number. The ceiling is a real bound, because `max_steps` caps every run.
  - Wall time is about 2 to 3 hours for 25 (an image pull, 0.5 to 4 minutes of agent time and some scoring per instance).
  - With 25 instances, a resolved rate has a 95% interval of roughly ±20 percentage points, so only large effects will show. Step 8e compares on the same instances, which helps.
  - gpt-6-luna would cost about half as much, but it would be a different agent, and every finding so far was measured on gpt-5.6-luna. Steps 8d and 8e must use the same model, or the comparison would mix two changes.
- **Docker disk.** Docker reports 860 GB free, and each image is about 4 GB, so 25 images need about 100 GB. This corrects the Step 8b estimate that removing images would be necessary past 10 to 20 instances. On this machine it is not needed at this scale.
- **`sample_instances.py` draws the sample.** `sample_ids` sorts the candidate ids before sampling, so the result depends only on the set of ids and the seed, not on the dataset's row order. It uses `random.Random(seed)`, a private generator that nothing else can advance. It never changes the caller's list, and it raises `ValueError` for an excluded id that is not in the dataset (most likely a typo) or for `n` larger than the ids left. The dataset and split come from the config, so the sample is drawn from the data the batch validates against.
- **The ids file is the source of truth, and the seed is a record.** Python does not promise that `random.sample` gives the same result across versions. The file starts with `#` lines giving the seed, dataset, split, `n`, exclusions and creation time, so the sample can be traced and redrawn. `run_batch.read_instances_file` skips `#` lines. `write_ids_file` creates `path.parent` and opens with mode `"x"`.
- **`instances/` is a new top-level folder.** The ids file is an input to batches and must be tracked. `config/` holds run settings and code (it is a Python package), and `runs/` holds gitignored outputs.
- **The dev set is for tuning.** Once Step 8e is tuned against it, any number reported afterwards needs a separate held-out set.
- **`HF_TOKEN` in `.env`.** `run_instance` calls `load_instance` once per instance, and each call checks the Hugging Face Hub even when the data is cached. That call is outside the `try` that records errors, so a refused request would stop the batch instead of being recorded. A token raises the rate limit. `class_agent.py` already loads `.env` with `load_dotenv()`, and `huggingface_hub` reads `HF_TOKEN` when it makes a request, so no code change was needed. `.env` is gitignored. `HF_HUB_OFFLINE=1` would avoid the Hub entirely, but it must be set in the shell, because it is read when `datasets` is imported, before `.env` is loaded.

### Bugs found in review

- **`sample_ids`:** `all_ids.pop(instance_id)` passed a value to `list.pop`, which takes a position (`TypeError`), and would have changed the caller's list. The fix builds a separate `candidates` list from `sorted(all_ids)`. One error message had a stray `f` inside the string, so it printed `candidates f19`.
- **`write_ids_file`:**
  - It first called `path.write_text` inside the `with` block. That method opens the file itself, in mode `"w"`, and replaces the whole file on every call. The block also had no `as f`. The fix writes through `f.write`.
  - A doubled `"\n"` put a blank line after each header line.
  - The parent folder was hardcoded (`Path("run/instances")`, then `Path("instances")`), so writing anywhere else raised `FileNotFoundError`. The fix is `path.parent.mkdir(...)`.
- **`main()`:**
  - `load_dataset(name, split)` passed the split as the second positional argument, which is the sub-configuration name (`BuilderConfig 'test' not found`), and unpacked the single returned `Dataset` into two names. The next version passed the dataset as `name=`, leaving the required `path` empty.
  - `write_ids_file(args.config, ...)` would have written the sample over `config/default.yaml`. Mode `"x"` refused, and the file was checked unchanged.
  - `args.excluded` for the attribute `exclude`.
  - `f"dataset={dataset}"` printed the `Dataset` object instead of its name.
  - `Counter(ids)` counted each id once instead of counting the repo prefixes.
- **`read_instances_file`** did not skip `#` lines, so the header would have been rejected as unknown ids.

### Verification

`sample_ids` on a fake list of 20 ids: the same seed gives the same sample and another seed a different one, the output is sorted, the caller's list is unchanged, an excluded id was never picked across 200 seeds, and both `ValueError` cases fire.

`write_ids_file` created two missing folders, wrote the header lines and ids, and raised `FileExistsError` when run again. A file with an indented comment and a blank line read back through `read_instances_file` as just its ids.

The real draw was repeated with seed 1234 to a scratch path and gave the same 25 ids as `instances/dev_25_seed1234.txt`. That file reads back as 25 ids, without `sympy__sympy-15599`, and passes `validate_instance_ids`. With the token in `.env`, the Hub authenticated the request and printed no warning.

## Step 8d fix: Capturing the patch against the start commit

**Goal.** Make `model_patch` hold only what the agent changed. The first 25-instance batch (`runs/2026-10-27T06-04-48`) showed that some patches also held changes made by the image itself.

### The finding

Five of the 25 patches were 60k to 234k characters: astropy-14182, astropy-14995, pytest-7432, sympy-21847 and sympy-24213. Almost all of each was mode-only changes (`old mode 100644`, `new mode 100755`), 539 to 1,929 files per patch, and none of the agent's commands changed file modes.

The cause is in the images. Each has an extra "SWE-bench" commit on top of the dataset's `base_commit`. Step 5 checked one image (sympy-15599), where that commit is empty, and concluded that diffing against `base_commit` was safe. For astropy-14182 the commit is not empty:

```
4331a8dde SWE-bench          HEAD
a5917978b Merge pull ...     base_commit
HEAD vs base_commit: 1862 mode changes, 1862 files changed, 1 insertion(+), 1 deletion(-)
```

A fresh container from that image shows no changes against HEAD, so the agent did not cause them. Diffing against `base_commit` put the image's own commit into the patch.

**Effect on the first batch: none of the outcomes changed.** In the two astropy instances, `git apply` failed on the image's `pyproject.toml` edit, which is already present in the evaluation container. The harness then fell back to `patch --fuzz` and applied the agent's change cleanly (`patch_successfully_applied: true`). The other three applied with type warnings only. Four of the five resolved. astropy-14182 failed for a real reason: the agent fixed writing tables with `header_rows` but not reading them back. Those scores relied on the harness's fallback working around patches it could not apply, so the fix was still needed.

### Design decisions

- **`read_start_commit(env)` runs `git rev-parse HEAD` after `env.start()` and before `agent.run()`.** The commit at container start leaves out the image's changes and still includes anything the agent commits itself. Diffing against `HEAD` at capture time would miss the agent's commits, which is why Step 5 avoided it.
- **The hash is stripped and the patch is not.** The trailing newline after a hash would end up inside the next git command. A patch needs its trailing newline (Step 5).
- **An unknown start commit is a failed capture.** `read_start_commit` returns `None` on a nonzero exit or a timeout, and `capture_patch` returns `None` for a `None` start commit without running git. Falling back to `base_commit` would quietly bring the bug back.
- **`start_commit` is recorded in every trajectory**, so it is always visible what a patch was diffed against, and a `None` explains a failed capture.
- **Both capture calls use it**, including the partial capture after a crash. A crashed run's patch is never scored, but its trajectory should not show the image's changes as the agent's.

### Bugs found in review

- **`read_start_commit`:**
  - The timeout branch printed a warning but had no `return None`, so it would have read a variable that was never set.
  - The nonzero-exit warning left out git's message, and a stray `f` inside an f-string printed the exit code as `f128`.
- **`run_instance`:** only the normal capture call was switched to `start_commit` at first. A stub crash on the astropy-14182 image then captured 1,862 files.
- **Docstring:** `capture_patch` kept "Verified:" in front of a statement that was now a design choice, and named its parameter `base_commit` after the rename.

### Verification

A reproduction script builds a temporary repo shaped like the astropy image (a base commit, then a "SWE-bench" commit that makes the files executable and edits `pyproject.toml`). It runs on `LocalEnvironment` and calls `read_start_commit` and `capture_patch`. On the old code all three cases failed. Case 3 also showed that a run with no agent changes would have had a non-empty patch and never scored as `empty_patch`.

| Case | Before | After |
|---|---|---|
| The agent edits one file | the image's mode changes and `pyproject.toml` in the patch | only the agent's file |
| The agent edits and commits | the image's changes included | the commit captured, nothing else |
| The agent changes nothing | a non-empty patch | `""` |

Against Docker, with `Agent.run` replaced by a stub:

| Run | Result |
|---|---|
| astropy-14182, edit then submit | `start_commit` is the image's SWE-bench commit, and the patch holds only `rst.py`, with 0 `old mode` lines |
| astropy-14182, edit then crash | the same |
| sympy-15599, full `main()` | `Submitted`, a one-file patch, `start_commit` recorded |
| `env.start()` raises | `Error`, `start_commit: None`, no capture attempted |

`read_start_commit` outside a git repo returns `None` with git's message, and `capture_patch(env, None)` returns `None` without running git. No container was left behind.

## Step 8d results: The baseline

**Goal.** Measure how often the current harness resolves the dev set, and how much that number moves between identical runs.

### Runs

All runs used gpt-5.6-luna and `config/default.yaml` on `instances/dev_25_seed1234.txt`. All 100 instance runs ended `Submitted`, with no `agent_error`, `capture_failed`, `error`, infra or ambiguous labels, and no command timeouts.

| Run | Notes | Resolved | Cost |
|---|---|---|---|
| `2026-10-27T06-04-48` | first batch, before the capture fix (outcomes checked unaffected) | 14 | $0.24 |
| `2026-10-27T09-09-31` | capture fix, run alone | 16 | $0.21 |
| `2026-10-27T18-03-42` | run in parallel with the Step 8e variant | 18 | $0.26 |
| `2026-10-27T18-43-12` | run in parallel with the Step 8e variant | 16 | $0.22 |

**The baseline is 64 of 100 instance runs resolved (64%).** Identical runs ranged from 14 to 18, so a single 25-instance run moves by about ±2 instances by chance. Between the first two runs, 4 of the 25 instances flipped (3 to resolved, 1 to unresolved). A difference of 1 to 3 instances between two single runs is therefore not evidence of anything. Running in parallel did not cause command timeouts in either arm.

Per instance, across the 4 runs:
- 13 instances resolved every time.
- 6 failed every time: astropy-13398, astropy-14182, django-11265, django-11477, sphinx-7590 and sympy-21596. These also failed in both Step 8e runs.
- The rest were unstable: django-11333, django-13569, sphinx-9230 and sympy-18763 resolved 2 of 4, django-15103 3 of 4, and xarray-2905 1 of 4.

### Why the unresolved runs failed

The 11 unresolved instances of the first batch were read from their trajectories, scores and the dataset's hidden test patches.

- **10 of the 11 fail a hidden FAIL_TO_PASS test.** The test is new in the hidden test patch, or the patch changes an existing test, so the agent could not have run the failing version whatever the prompt says. These are mostly incomplete fixes. astropy-14182, for example, fixed writing tables with `header_rows` but not reading them back.
- **3 broke an existing PASS_TO_PASS test (a regression):** astropy-13398, django-13569 and sphinx-9230. An earlier count of 4 was wrong. sympy-18763 is P2P 141/141. Running existing tests could have caught these three. astropy-13398 ran the right tests, saw `1 failed`, and exited anyway.
- **Running tests did not separate resolved from unresolved runs.** In the first batch, 7 of the 14 resolved runs used only scratch checks. The Step 7 pattern on sympy-15599 (every resolved run had run the tests, no unresolved run had) did not hold across 25 instances.
- **Finding the runner is a practical gap.** The sympy images have no `pytest`, so `python -m pytest` fails with `No module named pytest`. Sympy's runner is `bin/test` and Django's is `tests/runtests.py`. SWE-bench's `eval_script` must not be given to the agent, because it contains the hidden tests.

The test-runner counts come from a text search of the agent's commands, which cannot always tell a repository test run from pytest on a scratch file.

### The Step 6b narration problem continues

The prompt changes of Step 6b did not stop the model from writing out a whole session in its first reply. The `PrematureExit` guard rejected an early `exit` in 3 to 7 of the 25 runs of every batch. Replies with several bash blocks are still common and are rejected by the format check. Step 8e (below) shows the cost: a run whose edits were all in rejected replies can still pass every check and submit nothing. The guard only applies while no command has run. Fixing this is open.

## Step 8e: Exit-rule variant

**Goal.** Test the Step 7 idea that pointing the exit rule at the repository's own tests improves the result.

### The variant

`config/exit_tests.yaml` is `default.yaml` with two prompt lines changed. The rest is identical, which was checked by loading both files.

- **Exit rule:** exit only after (1) seeing output that verifies the fix and (2) running the repository's existing tests for the code that was changed and seeing them pass. Failures caused by the change must be fixed first.
- **Workflow line:** after verifying, find and run the existing tests, and if `pytest` is not installed, look for the project's own runner (for example a `runtests.py` or `bin/test` script). The hint is generic, and nothing from the evaluation script is included.
- **Not included:** advice to `git stash` a change to check whether a failure already existed, because forgetting `stash pop` would leave an empty patch.

The two changes are treated as one intervention, because the exit rule is useless when the agent cannot find the runner.

### Design

- **Paired runs.** Each variant run was started alongside a baseline run, a few seconds apart (folder names are per second), so both arms shared the same CPU load. This was done twice. No command timed out in any run.
- **The size of effect it can detect.** Two variant runs against four baseline runs can show a large effect or harm, but not a gain of 1 to 3 instances, which is the size of the run-to-run noise. That limit was accepted before running.

### Results

| Arm | Resolved per run | Total | Runs breaking a PASS_TO_PASS test | Cost per run |
|---|---|---|---|---|
| Baseline | 14, 16, 18, 16 | 64/100 (64%) | 7/100 | about $0.24 |
| Variant (`2026-10-27T18-03-48`, `18-43-18`) | 16, 16 | 32/50 (64%) | 2/50 | about $0.31 |

- **The resolved rate did not change.** Four instances differed between the arms, two in each direction, which is what noise looks like at this size.
- **The behaviour changed.** The variant ran a test runner in 24 and 25 of 25 runs, against 14 to 20 in the baseline, and most variant runs finished with a test run. It took about 20% longer and cost about 25% more. It still tried `pytest` first in the sympy images.
- **Regressions went down, from 7% of runs to 4%.** Both remaining ones were astropy-13398, which broke tests in every run of both arms. The variant removed the regressions in django-13569 and sphinx-9230, but neither became reliably resolved, because both also fail their hidden FAIL_TO_PASS test in some runs.
- **None of the 6 always-failing instances was rescued.**

### Conclusion

**The exit rule only helps where the hidden FAIL_TO_PASS tests already pass.** It reduces regressions, but a regression only costs the score when the fix is otherwise correct, as in the django-13569 runs whose only failure was a regression. In this dev set that is rare, so the rule costs about 25% more and has no measurable effect on the score. It stays out of `default.yaml`, and `exit_tests.yaml` is kept. It becomes worth revisiting once incomplete fixes are less common, because then more failures would be regression-only.

### A new failure mode

In the first variant run, django-11333 submitted an empty patch. Its edits were in two replies with 12 and 4 bash blocks, which the format check rejected, so no edit ever ran. It then ran `tests/runtests.py urlpatterns_reverse`, which passed on the unchanged code, ran a verification script, and exited. The new rule was met by an unchanged repository, and the `PrematureExit` guard did not apply because commands had run. This is the Step 6b narration problem.

### Open items

- **The narration problem (Steps 6b and 8d).** The first reply still narrates a session, and replies with many blocks are still common. Both waste steps, and they can end in an empty submission.
- **Incomplete fixes** are the main cause of failure: 6 instances failed in all 6 runs, all on hidden tests.
- **Larger runs.** A larger dev-set baseline (100 to 150 instances, about 400 to 600 GB of images) would narrow the 95% interval from about ±20 to about ±8 to 10 points. The full 500 instances would need about 2 TB of images, so it first needs each image removed after its run (deferred in Step 8b).
