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
        ValueError: if preds.jsonl is empty (for example if manually edited or batch runner
        creates prediction jsonl files before an instance finishes).

    Why:
        Both run_harness (needs the model name to find the summary file) and
        collect_scores (needs null vs "" for each patch) read this file. One
        reader means one place that knows its format.
    """

    path = run_dir / "preds.jsonl"
    predictions = {}

    with path.open("r", encoding="utf-8") as f:

        for line in f:
            if line.strip():
                prediction = json.loads(line)
                prediction_id = prediction["instance_id"]

                predictions[prediction_id] = prediction

    if not predictions:
        raise ValueError(f"preds.jsonl located at {path} is empty.")

    return predictions

def safe_model_name(model_name: str) -> str:

    """

    Return model_name in a path safe format.

    Args:
        model_name the model_name_or_path from preds.jsonl

    Returns: model_name: str

    Why:

        In its initial form the model_name would cause any path usage to break in the py file.
        Switching the "/" to "__" fixes this and ensures the path can be appropriately written
        in the exact harness format and then called.

    """

    return model_name.replace("/","__")


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

    return run_dir / f"{safe_model_name(model_name)}.{run_dir.name}.json"


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

    predictions = read_predictions(run_dir)
    model_name = next(iter(predictions.values()))["model_name_or_path"]

    argv = [
        sys.executable, "-m", HARNESS_MODULE,
        "--dataset_name", dataset_name, "--split", split,
        "--predictions_path", "preds.jsonl",
        "--run_id", run_dir.name,
        "--instance_ids", *predictions,
        "--max_workers", str(max_workers), "--timeout", str(timeout),
        "--report_dir", "."
    ]

    output = subprocess.run(argv, cwd=run_dir, check=False)

    if output.returncode != 0:
        raise RuntimeError(f"Harness exited with code {output.returncode}")

    report = harness_report_path(run_dir, model_name)

    if not report.exists():
        raise RuntimeError("No harness report path exists")

    return report

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

    summary = json.loads(harness_report.read_text(encoding="utf-8"))
    predictions = read_predictions(run_dir)
    model_name = next(iter(predictions.values()))["model_name_or_path"]

    instances = {}
    counts = {status: 0 for status in STATUSES}

    for instance_id, prediction in predictions.items():
        if instance_id in summary["empty_patch_ids"]:

            if prediction["model_patch"] is None:
                status = "capture_failed"

            else:
                status = "empty_patch"

        elif instance_id in summary["resolved_ids"]:
            status = "resolved"

        elif instance_id in summary["unresolved_ids"]:
            status = "unresolved"

        elif instance_id in summary["error_ids"]:
            status = "error"

        else:
            raise ValueError(f"No instance id for {instance_id} is included in the harness id list")

        report_path = run_dir / HARNESS_LOG_DIR / run_dir.name / safe_model_name(model_name) / instance_id / "report.json"
        counts[status] += 1

        fail_to_pass = None
        pass_to_pass = None

        if report_path.exists():

            try:

                instance_report = json.loads(report_path.read_text(encoding="utf-8"))

                fail_to_pass = instance_report[instance_id]["tests_status"]["FAIL_TO_PASS"]
                pass_to_pass = instance_report[instance_id]["tests_status"]["PASS_TO_PASS"]


            except(json.JSONDecodeError, KeyError):
                pass

        instances[instance_id] = {
            "status": status,
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass,

        }

    swebench_version = version("swebench")

    return {
        "run_id": run_dir.name, "dataset": dataset_name, "split": split,
        "swebench_version": swebench_version,
        "summary": counts, "instances": instances
    }
