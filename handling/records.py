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
    prediction = {
        "instance_id": record["instance_id"],
        "model_name_or_path": record["model"],
        "model_patch": record["model_patch"]
    }

    line = json.dumps(prediction, ensure_ascii=False)
    path = run_dir / "preds.jsonl"

    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# TODO (Stage 7): add "scores.json  written by score_run.py (Stage 7)" to the
#   Layout block in this file's header, and note that the harness's own
#   <model>.<run_id>.json and logs/ also land in the run folder (see scoring.py).
def save_scores(run_dir: Path, scores: dict) -> Path:
    """
    Write scores.json: one status and the test results per scored instance.

    Args:
        run_dir: The scored run folder.
        scores: The dict returned by scoring.collect_scores().

    Returns:
        Path of the written file, run_dir / "scores.json".

    Raises:
        FileExistsError: if the run was already scored.

    Why:
        Lives here because this module owns everything a run leaves on disk.
        Mode "x" follows the Step 6 rule that nothing is silently overwritten.
        To rescore, delete scores.json first (the harness itself skips
        instances that already have a report.json for this run id, so the
        old logs/ folder must go too for a true rerun).
    """
    # TODO: same pattern as save_trajectory: open with mode "x" and
    #       encoding="utf-8", json.dump(scores, f, indent=2, ensure_ascii=False),
    #       return the path.
    raise NotImplementedError
