# =============================================================================
# scoring.py
# -----------------------------------------------------------------------------
# Responsible for: Scoring a finished run folder with the official SWE-bench
#                  harness, and turning the harness's reports into one status
#                  per instance (plus which tests passed and failed).
# Role in project: Called by score_run.main() after a run folder exists
#                  (written by run_single.py). Separate from running the
#                  agent: scoring is slow, needs no API calls, and can be
#                  rerun on an old run folder without rerunning the agent.
# Layout added:    runs/<run_id>/
#                      <model>.<run_id>.json      harness summary (all instances)
#                      logs/run_evaluation/<run_id>/<model>/<instance_id>/
#                          report.json            per-instance test results
#                          test_output.txt        raw test log
#                      scores.json                our summary (records.save_scores)
# Assumptions:     swebench 5.x installed in the same Python (sys.executable).
#                  The dataset has the 5.x fields (image, eval_script,
#                  log_parser): SWE-bench/SWE-bench_Verified, not princeton-nlp.
#                  Every line of preds.jsonl has the same model_name_or_path
#                  (the harness names its summary file after the first line).
#                  Docker running, and the instance images pull or exist locally.
# =============================================================================

import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

HARNESS_MODULE = "swebench.harness.run_evaluation"

# The harness writes per-instance logs under this path, relative to its working
# directory (swebench.harness.constants.RUN_EVALUATION_LOG_DIR in 5.0.2).
HARNESS_LOG_DIR = Path("logs") / "run_evaluation"

# One status per instance. Kept distinct on purpose: the harness merges a
# failed capture (model_patch null) into "empty patch", and "error" (the
# harness could not evaluate) is not the same as "unresolved" (tests failed).
# The harness's own "incomplete" means "in the dataset but not predicted", so
# it never applies to an instance in preds.jsonl and is not a status here.
STATUSES = ("resolved", "unresolved", "empty_patch", "capture_failed", "error")


def read_predictions(run_dir: Path) -> dict[str, dict]:
    """
    Load preds.jsonl from a run folder, keyed by instance_id.

    Args:
        run_dir: A run folder from records.make_run_dir().

    Returns:
        {instance_id: prediction dict}, each with instance_id,
        model_name_or_path and model_patch (str or None).

    Raises:
        FileNotFoundError: if the run folder has no preds.jsonl (for example a
            run that crashed before append_prediction).

    Why:
        Both run_harness (needs the model name to find the summary file) and
        collect_scores (needs null vs "" for each patch) read this file. One
        reader means one place that knows its format.
    """
    # TODO 1: path = run_dir / "preds.jsonl". Open it with encoding="utf-8"
    #         (a missing file raises FileNotFoundError by itself, so no
    #         separate exists() check is needed).
    # TODO 2: one json.loads per non-empty line (JSONL: one object per line).
    #         Skip blank lines, e.g. a trailing "\n" read as an empty line.
    # TODO 3: return a dict keyed by each prediction's "instance_id".
    raise NotImplementedError


def harness_report_path(run_dir: Path, model_name: str) -> Path:
    """
    Return where the harness writes its summary report for this run.

    Args:
        run_dir: The run folder, also passed to the harness as --report_dir.
        model_name: The model_name_or_path from preds.jsonl.

    Returns:
        run_dir / "<model_name with '/' replaced by '__'>.<run_dir.name>.json".

    Why:
        The harness builds this name itself (reporting.make_run_report) and
        only prints it. Rebuilding it here, in one place, lets the caller find
        the file without parsing the harness's printed output. If a swebench
        upgrade changes the naming, this is the one function to update.
    """
    # TODO: build the file name exactly as described in Returns. The "/" -> "__"
    #       replacement matters for model names like "openai/gpt-...".
    raise NotImplementedError


def run_harness(
    run_dir: Path,
    dataset_name: str,
    split: str,
    max_workers: int = 1,
    timeout: int = 1800,
) -> Path:
    """
    Score every prediction in a run folder with the official SWE-bench harness.

    Args:
        run_dir: A run folder containing preds.jsonl.
        dataset_name: Data the harness reads FAIL_TO_PASS, PASS_TO_PASS, the
            image name and the eval script from (e.g. "SWE-bench/SWE-bench_Verified").
        split: Dataset split, e.g. "test".
        max_workers: Instances the harness evaluates in parallel.
        timeout: Seconds the harness allows for one instance's tests.

    Returns:
        Path to the harness summary report (<model>.<run_id>.json in run_dir).

    Raises:
        FileNotFoundError: if preds.jsonl is missing.
        RuntimeError: if the harness exits nonzero, or exits 0 but the summary
            report is not where harness_report_path() says.

    Why:
        Calls the harness through its command line, not its Python functions:
        the CLI is its public interface, while its internals changed between
        major versions. The run folder's name is the harness run id, and the
        harness runs with cwd=run_dir, so its logs land inside the run folder
        and everything about one run stays in one place.
    """
    # TODO 1: predictions = read_predictions(run_dir). Take model_name from any
    #         prediction (all lines share it, see the file header).
    # TODO 2: build the argv as a list (no shell=True):
    #           [sys.executable, "-m", HARNESS_MODULE,
    #            "--dataset_name", dataset_name, "--split", split,
    #            "--predictions_path", <preds.jsonl as an ABSOLUTE path>,
    #            "--run_id", run_dir.name,
    #            "--instance_ids", *<the instance ids in predictions>,
    #            "--max_workers", str(max_workers), "--timeout", str(timeout),
    #            "--report_dir", <run_dir as an ABSOLUTE path>]
    #         Absolute paths because cwd changes to run_dir: a relative path
    #         would then be resolved twice ("runs/x/runs/x/preds.jsonl").
    #         sys.executable = the Python running this script, so the harness
    #         runs in the same venv without a hard-coded .venv/bin path.
    #         --instance_ids limits the harness to this run's instances.
    #         Without it, the summary lists every other dataset instance as
    #         "incomplete" (499 of 500 for a single-instance run).
    # TODO 3: subprocess.run(argv, cwd=run_dir, check=False). Do NOT capture
    #         output: the harness prints progress for minutes, and seeing it
    #         live is the point. No subprocess timeout either: the harness
    #         enforces --timeout per instance. (Stage 8 can add an overall one.)
    # TODO 4: nonzero returncode -> raise RuntimeError naming the returncode.
    # TODO 5: report = harness_report_path(run_dir, model_name). If it does not
    #         exist, raise RuntimeError (the naming assumption broke). Else return it.
    raise NotImplementedError


def collect_scores(
    run_dir: Path, harness_report: Path, dataset_name: str, split: str
) -> dict:
    """
    Combine the harness reports with preds.jsonl into one status per instance.

    Args:
        run_dir: The scored run folder.
        harness_report: Path returned by run_harness().
        dataset_name: The dataset the harness scored against (recorded, since
            it can differ from the one in run.json via --dataset-name).
        split: The split the harness scored against.

    Returns:
        {"run_id": str, "dataset": str, "split": str, "swebench_version": str,
         "summary": {status: count for every status in STATUSES},
         "instances": {instance_id: {
             "status": one of STATUSES,
             "fail_to_pass": {"success": [...], "failure": [...]} | None,
             "pass_to_pass": {"success": [...], "failure": [...]} | None}}}
        The test fields are None when no tests ran (empty patch, capture
        failed, or an error before a report was written), which is
        different from empty lists.

    Raises:
        ValueError: if an instance in preds.jsonl is in none of the harness's
            id lists (our reading of the report format is wrong).

    Why:
        The harness summary answers "resolved or not". This adds what it
        drops: capture_failed vs empty_patch (null vs ""), and which tests
        failed, so an unresolved instance can be diagnosed from one file.
        swebench_version is recorded because the scorer's version decides the
        result, the same way run.json records the harness commit.
    """
    # TODO 1: summary = json.loads(harness_report.read_text(encoding="utf-8")).
    #         The id lists used below: "resolved_ids", "unresolved_ids",
    #         "error_ids", "empty_patch_ids". In 5.0.2 a predicted instance is
    #         in exactly one of them (reporting.make_run_report checks empty
    #         patch first, then report.json), so the order of checks below
    #         does not change the result.
    # TODO 2: predictions = read_predictions(run_dir).
    # TODO 3: for each instance_id in predictions, pick its status:
    #           - in empty_patch_ids: "capture_failed" if its model_patch is None,
    #             else "empty_patch"
    #           - in resolved_ids / unresolved_ids / error_ids: that status
    #           - in none of them: raise ValueError (see Raises)
    # TODO 4: tests: the per-instance report is at
    #           run_dir / HARNESS_LOG_DIR / run_dir.name /
    #           <model_name with "/" -> "__"> / instance_id / "report.json"
    #         It is shaped {instance_id: {..., "tests_status": {"FAIL_TO_PASS":
    #         {"success", "failure"}, "PASS_TO_PASS": {...}}}}.
    #         If it exists, copy the two dicts. If not, set both to None.
    #         (An error instance may have a report that is empty or not JSON:
    #         catch json.JSONDecodeError and KeyError there and use None.)
    # TODO 5: summary counts: one entry per status in STATUSES, including zeros,
    #         so every scores.json has the same keys.
    # TODO 6: swebench_version = version("swebench") (importlib.metadata).
    # TODO 7: return the dict described in Returns.
    raise NotImplementedError
