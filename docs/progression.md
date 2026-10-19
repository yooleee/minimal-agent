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
| 6 | Run record (config, messages, result) saved to disk | Next |
| 7 | Scoring with the SWE-bench harness | Planned |
| 8 | Robustness and batch runs | Planned |

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
- **`git add -A`, then `git diff --cached <base_commit>`.** Plain `git diff` skips files the agent created. Diffing against `base_commit` instead of HEAD still catches changes if the agent runs `git commit`. Checked in the image: HEAD is an extra empty "SWE-bench" commit on top of `base_commit`, and the tree starts clean.
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
