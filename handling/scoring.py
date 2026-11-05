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
# failed capture (model_patch null) and agent_error into "empty patch", and "error" (the
# harness could not evaluate) is not the same as "unresolved" (tests failed).
# The harness's own "incomplete" means "in the dataset but not predicted", so
# it never applies to an instance in preds.jsonl and is not a status here.
STATUSES = ("resolved", "unresolved", "empty_patch", "capture_failed", "agent_error", "error")


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


def evaluation_states(run_dir: Path, predictions: dict[str, dict]) -> dict[str, list[str]]:
    """
    Sort the predicted ids by whether the harness has evaluated them yet.

    Args:
        run_dir: A run folder containing preds.jsonl.
        predictions: The result of read_predictions(run_dir).

    Returns:
        {"unevaluated": [ids with a patch and no instance log folder],
         "no_report": [ids with an instance log folder but no report.json]},
        each in preds.jsonl order. Ids with an empty or null patch, and ids
        with a report.json, are in neither list.

    Why:
        The harness skips an id only if its report.json exists. A test
        timeout or a crash during evaluation writes the log folder
        (logs/run_evaluation/<run_id>/<model>/<id>/, created before the tests
        start) but no report, so passing every id evaluated it again on
        every call (sympy-19040 in Step 9f, 30 min each time). The folder on
        disk tells "never evaluated" apart from "evaluated and failed", and
        unlike a list kept in memory it survives a resume. Empty patches are
        left out because the harness never evaluates them.
    """
    unevaluated_ids = []
    id_no_report = []
    model_name = next(iter(predictions.values()))["model_name_or_path"]

    for instance_id, prediction in predictions.items():

        if (prediction["model_patch"] == "") or (prediction["model_patch"] is None):
            continue

        log_dir = run_dir / HARNESS_LOG_DIR / run_dir.name / safe_model_name(model_name) / instance_id

        if not log_dir.exists():
            unevaluated_ids.append(instance_id)

        elif not (log_dir / "report.json").exists():
            id_no_report.append(instance_id)

    return {
        "unevaluated": unevaluated_ids,
        "no_report": id_no_report
    }

def run_harness(
    run_dir: Path,
    dataset_name: str,
    split: str,
    max_workers: int = 1,
    timeout: int = 1800,
    instance_ids: list[str] | None = None,
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
        instance_ids: The ids to evaluate. None (the default, used by
            score_run.py) means every id in preds.jsonl.

    Returns:
        Path to the harness summary report (<model>.<run_id>.json in run_dir).
        It covers only the ids passed, so after a call with instance_ids it
        is not a summary of the whole run until score_run.py rewrites it.

    Raises:
        FileNotFoundError: if preds.jsonl is missing.
        ValueError: if instance_ids is an empty list.
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

    if instance_ids == []:
        raise ValueError("The harness reads every id as empty because --instance_ids is an empty list")

    if instance_ids is None:
        usable_ids = list(predictions)

    else:
        usable_ids = instance_ids

    argv = [
        sys.executable, "-m", HARNESS_MODULE,
        "--dataset_name", dataset_name, "--split", split,
        "--predictions_path", "preds.jsonl",
        "--run_id", run_dir.name,
        "--instance_ids", *usable_ids,
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
             "pass_to_pass": {"success": [...], "failure": [...]} | None
             "failure_label": "infra", "ambiguous" | None
             "failure_reason": str | None }}}

        The test fields are None when no tests ran (empty patch, capture
        failed, agent_error, or an error before a report was written), which is
        different from empty lists.

        None has two meanings that status tells apart. For unresolved
        and error it means "classified, no infrastructure signal". For every
        other status the harness never classifies, so the label does not apply.

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

            # agent_error first: a crashed run also has model_patch null.
            # .get() because preds.jsonl from before Step 8a has no such key.
            if prediction.get("agent_error") is not None:
                status = "agent_error"

            elif prediction["model_patch"] is None:
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

        # The harness classifies only unresolved and error instances (swebench
        # 5.0.2 reporting.py). The label lists use [] so a changed report format
        # fails loudly. failure_reasons only holds classified ids, hence .get().
        if instance_id in summary["infra_failure_ids"]:
            failure_label = "infra"

        elif instance_id in summary["ambiguous_failure_ids"]:
            failure_label = "ambiguous"

        else:
            failure_label = None

        failure_reason = summary["failure_reasons"].get(instance_id)

        instances[instance_id] = {
            "status": status,
            "fail_to_pass": fail_to_pass,
            "pass_to_pass": pass_to_pass,
            "failure_label": failure_label,
            "failure_reason": failure_reason

        }

    swebench_version = version("swebench")

    return {
        "run_id": run_dir.name, "dataset": dataset_name, "split": split,
        "swebench_version": swebench_version,
        "summary": counts, "instances": instances
    }
