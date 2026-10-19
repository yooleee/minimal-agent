# =============================================================================
# records.py
# -----------------------------------------------------------------------------
# Responsible for: Writing everything a run leaves on disk: the run folder,
#                  run.json (config + harness version), one trajectory per
#                  instance, and the predictions file for scoring.
# Role in project: Called by run_single.main() after run_instance() returns.
#                  Kept out of run_single.py so the Stage 8 batch runner can
#                  import the same functions with one shared run_dir.
# Layout:          runs/<UTC timestamp>/
#                      run.json                    written once, before any instance
#                      <instance_id>.traj.json     one per instance
#                      preds.jsonl                 one line appended per instance
# Assumptions:     Records are plain dicts from run_instance() holding only JSON-
#                  serializable values (str, int, float, None, list, dict).
#                  `git` on PATH for the harness version (recorded as None if not).
# =============================================================================

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def make_run_dir(base: Path) -> Path:
    """
    Create a new, empty folder for this run, named by the current UTC time.

    Args:
        base: Parent folder for all runs (e.g. <project>/runs).

    Returns:
        Path to the new run folder, e.g. runs/2026-10-19T15-30-12.

    Raises:
        FileExistsError: if a folder with that name already exists (two runs
            started in the same second).

    Why:
        One folder per invocation keeps runs from mixing. Failing on a name
        collision is explicit: silently reusing a folder would interleave two
        runs' predictions in one preds.jsonl.
    """
    # TODO 1: run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    #         Hyphens instead of colons in the time: ":" is not allowed in Windows
    #         file names, and this folder may be opened from WSL/Windows.
    #         The format also sorts alphabetically in time order.
    # TODO 2: make sure base exists: base.mkdir(parents=True, exist_ok=True)
    #         (exist_ok=True here: the runs/ folder is shared by every run)
    # TODO 3: run_dir = base / run_id, then run_dir.mkdir(exist_ok=False)
    #         (exist_ok=False here: this folder must be new)
    # TODO 4: return run_dir
    #

    run_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    base.mkdir(parents=True, exist_ok=True)

    run_dir = base / run_id
    run_dir.mkdir(exist_ok=False)

    return run_dir

def _run_git(args: list[str], repo: Path) -> str | None:

    """
    Run the git command and checks for git failures (OSError, Timeout, Nonzero Returncode).

    Args:
        args: The git command arguments / flags
        repo: The repo the command is being initiated at.

    Returns:
        Git command output returned as a string or None if git failed or could not run.

    Why:
        The helper deals with how git can fail when recording a run. Placing this process
        into a function removes needing to hard code the same try / except block across multiple
        calls. Instead the function is called when a git command is run and the output is returned
        if no failures occur.
    """

    try:

        git_run = subprocess.run(
            ["git", *args],
            cwd=repo, capture_output=True,
            text=True, timeout=10, check=False
        )

    except (OSError, subprocess.TimeoutExpired):
        return None

    if git_run.returncode != 0:
        return None

    return git_run.stdout

def _harness_git_state() -> dict:
    """
    Read the commit and dirty flag of the harness's own git repo.

    Returns:
        {"commit": str | None, "dirty": bool | None}. None means "could not
        check" (git missing, not a repo), which is different from a clean tree.

    Why:
        A run's results depend on the harness code as much as on the config.
        The commit pins which code ran. The dirty flag says whether uncommitted
        edits were also in play, in which case the commit alone can't reproduce it.
    """
    # TODO 1: repo = Path(__file__).resolve().parent.parent   (handling/ -> project root)
    #         so this works no matter which directory you launch from
    # TODO 2: subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
    #         text=True, timeout=10, check=False) -> commit = stdout.strip()
    # TODO 3: subprocess.run(["git", "status", "--porcelain"], cwd=repo, ...same...)
    #         -> dirty = (stdout.strip() != "")   (any line means a modified/new file)
    # TODO 4: if either call has a nonzero returncode, or raises OSError (git not
    #         installed) or TimeoutExpired, use None for that value. Don't raise:
    #         a missing version stamp shouldn't stop a run.
    # TODO 5: return {"commit": ..., "dirty": ...}
    #

    git_repo = Path(__file__).resolve().parent.parent

    outbound_commit = _run_git(["rev-parse", "HEAD"], git_repo)
    outbound_status = _run_git(["status", "--porcelain"], git_repo)

    commit = None if outbound_commit is None else outbound_commit.strip()
    dirty = None if outbound_status is None else (outbound_status.strip() != "")

    return {"commit": commit, "dirty": dirty}


def save_run_metadata(run_dir: Path, config: dict) -> None:
    """
    Write run.json: the resolved config plus how and when the run was started.

    Args:
        run_dir: Folder from make_run_dir().
        config: The loaded config dict (from load_config).

    Returns:
        None. Writes run_dir / "run.json".

    Why:
        Saves the config VALUES, not the YAML path, because the file can change
        after the run. Written before any instance runs, so even a run that
        crashes leaves a record of what it was trying to do.
    """
    # TODO 1: metadata = {
    #             "started_at": datetime.now(timezone.utc).isoformat(),
    #             "harness": _harness_git_state(),
    #             "python": sys.version,
    #             "argv": sys.argv,          (the exact command line)
    #             "config": config,
    #         }
    # TODO 2: write it with json.dump(..., indent=2, ensure_ascii=False) to
    #         run_dir / "run.json", opened with encoding="utf-8"
    #         (ensure_ascii=False keeps non-ASCII text readable instead of \u escapes,
    #         and the explicit encoding makes that safe on Windows too)

    metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "harness": _harness_git_state(),
        "python": sys.version,
        "argv": sys.argv,
        "config": config
    }

    path = run_dir / "run.json"
    with path.open("x", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)


def save_trajectory(run_dir: Path, record: dict) -> Path:
    """
    Write one instance's full record (including all messages) to its own file.

    Args:
        run_dir: Folder from make_run_dir().
        record: The dict returned by run_instance().

    Returns:
        Path of the written file, e.g. run_dir / "sympy__sympy-15599.traj.json".

    Raises:
        FileExistsError: if this instance already has a trajectory in run_dir.

    Why:
        The trajectory is how you inspect a run afterwards: what the agent tried,
        in what order, and where it stopped. One file per instance keeps each one
        small enough to open, and lets a batch save as it goes.
    """
    # TODO 1: path = run_dir / f"{record['instance_id']}.traj.json"
    # TODO 2: open with mode "x" (create, fail if it exists) and encoding="utf-8",
    #         then json.dump(record, f, indent=2, ensure_ascii=False)
    #         Why "x" and not "w": "w" would silently overwrite an earlier result for
    #         the same instance. Same reasoning as exist_ok=False in make_run_dir.
    # TODO 3: return path

    path = run_dir / f"{record['instance_id']}.traj.json"

    with path.open("x", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)

    return path



def append_prediction(run_dir: Path, record: dict) -> None:
    """
    Append one line to preds.jsonl in the SWE-bench predictions format.

    Args:
        run_dir: Folder from make_run_dir().
        record: The dict returned by run_instance().

    Returns:
        None. Appends to run_dir / "preds.jsonl".

    Why:
        These three keys are what the SWE-bench evaluation harness reads, so
        Stage 7 can score this file directly. JSONL (one JSON object per line)
        means each instance is one append: a batch that crashes halfway keeps
        every line already written, and nothing is ever rewritten.
    """
    # TODO 1: prediction = {
    #             "instance_id": record["instance_id"],
    #             "model_name_or_path": record["model"],
    #             "model_patch": record["model_patch"],
    #         }
    #         Keep model_patch as None when capture failed (it becomes null in JSON).
    #         Don't turn it into "": that would erase the "failed vs. no changes"
    #         distinction. Stage 7 checks how the harness treats null.
    # TODO 2: line = json.dumps(prediction, ensure_ascii=False)
    #         NO indent: JSONL needs each record on exactly one line.
    # TODO 3: open run_dir / "preds.jsonl" with mode "a" (append, create if missing)
    #         and encoding="utf-8", and write line + "\n"

    prediction = {
        "instance_id": record["instance_id"],
        "model_name_or_path": record["model"],
        "model_patch": record["model_patch"]
    }

    line = json.dumps(prediction, ensure_ascii=False)
    path = run_dir / "preds.jsonl"

    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
