# =============================================================================
# run_batch.py
# -----------------------------------------------------------------------------
# Responsible for: Running the agent on SEVERAL SWE-bench instances, one after
#                  another, into one shared run folder.
# Role in project: The batch entry point, next to run_single.py. Each instance
#                  goes through run_single.run_and_save(), so the run folder has
#                  the same layout as a single run (records.py header), with one
#                  trajectory per instance and one preds.jsonl line per instance.
#                  Score the folder afterwards with score_run.py.
# Assumptions:     Run from the project root. OPENAI_API_KEY in .env. Docker
#                  running. Instances run sequentially: under amd64 emulation on
#                  Apple Silicon, parallel containers mostly compete for CPU.
#                  Revisit with an x86 host or batches of more than ~50.
#                  --resume <run_dir> continues a stopped run in its own folder.
#                  The batch stops after MAX_CONSECUTIVE_ERRORS Errors in a
#                  row (exit code 1). Continue it with --resume.
#                  Before a long run:
#                    1. docker login, or decide to stay anonymous. Check the
#                       limit with a HEAD request to Docker Hub's
#                       ratelimitpreview/test manifest, which is not counted
#                       (ratelimit-limit and ratelimit-remaining headers).
#                    2. export HF_HUB_OFFLINE=1, so a slow or unreachable
#                       Hugging Face hub cannot stall each instance's dataset
#                       load (measured: 25 s to 7 min before the cache is used).
#                    3. Run under `caffeinate -i` so the Mac stays awake.
# =============================================================================

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from datasets import load_dataset

from config.environments import image_exists, remove_image, swebench_image_name
from handling.records import (
    append_resume,
    keep_previous_attempt,
    make_run_dir,
    save_run_metadata,
)
from handling.scoring import read_predictions, run_harness
from run_single import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_DIR,
    load_config,
    run_and_save,
)

# Max error value is 3 because a single error is normal noise while three in a row
# could represent an error or outage occurring.
MAX_CONSECUTIVE_ERRORS = 3


def read_instances_file(path: Path) -> list[str]:
    """
    Read instance ids from a text file, one id per line.

    Args:
        path: A UTF-8 text file. Blank and comment lines are skipped.

    Returns:
        The ids in file order, with surrounding whitespace removed.

    Why:
        A tracked file of ids is how a batch is repeated exactly (Stage 8d's dev
        set). Order is kept so two runs of the same file go in the same order.
    """
    instance_ids = []
    with path.open(encoding="utf-8") as f:

        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                instance_ids.append(line)

    return instance_ids

def validate_instance_ids(instance_ids: list[str], dataset_name: str, split: str) -> None:
    """
    Check every id before any container starts or any API call is made.

    Args:
        instance_ids: The ids the batch will run, in order.
        dataset_name: e.g. "SWE-bench/SWE-bench_Verified" (from the config).
        split: e.g. "test".

    Returns:
        None.

    Raises:
        ValueError: if the list is empty, has duplicates, or has ids not in the
            dataset. The message lists every bad id at once.

    Why:
        Without this, a typo in the 30th id fails only after 29 paid runs.
        Duplicates are rejected because save_trajectory opens with mode "x":
        the second run of an id would be paid for, then fail to save, and that
        error is outside run_instance's catch, so it would stop the batch.
    """
    if instance_ids == []:
        raise ValueError("Instance id list is empty.")

    duplicate_ids = []
    for instance_id, count in Counter(instance_ids).items():

        if count > 1:
            duplicate_ids.append(instance_id)

    if duplicate_ids != []:
        raise ValueError(f"Duplicates found in id list {duplicate_ids}")

    benchmark_dataset = load_dataset(dataset_name, split=split)
    instance_id_set = set(benchmark_dataset["instance_id"])

    unknown_ids = []
    for instance_id in instance_ids:

        if instance_id not in instance_id_set:
            unknown_ids.append(instance_id)

    if unknown_ids != []:
        raise ValueError(f"Unknown ids not found in set: {unknown_ids} from dataset {dataset_name} with split {split}")


def score_new_predictions(run_dir: Path, config: dict) -> bool:
    """
    Score the predictions in run_dir that have no harness report yet.

    Args:
        run_dir: The batch's run folder.
        config: The loaded config (for the dataset name and split).

    Returns:
        True if the harness ran, False if it failed (a warning is printed).

    Why:
        The image must still be present when an instance is scored, or the
        harness pulls it again. run_harness passes every id in preds.jsonl, but
        swebench 5.0.2 skips ids that already have a report.json for this run id
        (run_evaluation.get_dataset_from_preds), so only the new one is
        evaluated. A failure here must not stop a multi-day batch: score_run.py
        at the end evaluates any id that still has no report.
    """

    try:
        run_harness(run_dir, config["dataset"]["name"], config["dataset"]["split"])

    except RuntimeError as e:
        print(f"Scoring failed at {run_dir} due to {e}, retry will occur at the end.")
        return False

    return True


def load_resume_ids(run_dir: Path, config: dict) -> list[str]:
    """
    Read the planned ids of a run being resumed, refusing a different config.

    Args:
        run_dir: The run folder to resume (holds run.json).
        config: The config loaded from --config for this session.

    Returns:
        run.json's "instance_ids", in their original order.

    Raises:
        FileNotFoundError: if run_dir has no run.json (not a run folder).
        ValueError: if config differs from run.json's "config". The message
            names the top-level keys that differ.

    Why:
        One folder must never mix results from two configs: scores.json would
        average them and nothing would show it. The ids come from run.json, not
        the command line, so a resume cannot quietly run a different list.
        Comparing the loaded dicts works because run.json stores the config as
        JSON and YAML gives only JSON-compatible values here.
    """

    with (run_dir / "run.json").open(encoding="utf-8") as f:
        run_json = json.load(f)

    saved_config = run_json["config"]
    if saved_config != config:
        non_matching_keys = []

        for key in sorted(saved_config.keys() | config.keys()):
            if key not in config:
                non_matching_keys.append(key)

            elif key not in saved_config:
                non_matching_keys.append(key)

            elif saved_config[key] != config[key]:
                non_matching_keys.append(key)

        raise ValueError(f"Missing or Non-matching keys found: {non_matching_keys}")

    return run_json["instance_ids"]


def finished_ids(run_dir: Path) -> set[str]:
    """
    Return the ids whose latest prediction in preds.jsonl has no agent error.

    Args:
        run_dir: The run folder being resumed.

    Returns:
        The ids a resume skips. An empty set when preds.jsonl does not exist
        yet (the run stopped during its first instance).

    Why:
        Everything else is rerun: an id with agent_error set crashed (its patch
        is null and was never scored, since the harness skips null patches), and
        an id with no line never finished. read_predictions keeps the LAST line
        per id, the same rule the harness uses, so an errored attempt followed
        by a successful rerun counts as finished.
    """

    if not (run_dir / "preds.jsonl").exists():
        empty_set = set()
        return empty_set

    predictions = read_predictions(run_dir)
    finished_ids = []

    for instance_id, prediction in predictions.items():

        if prediction["agent_error"] is None:
            finished_ids.append(instance_id)

    return set(finished_ids)

def main() -> None:
    """
    Parse the command line, validate the ids, then run each instance in turn.

    Returns:
        None. Writes one run folder and prints a line per instance plus a tally.

    Why:
        Validation happens before make_run_dir, so a bad id list leaves no empty
        run folder behind. Each instance is saved as soon as it finishes (inside
        run_and_save), so stopping the batch keeps everything already done.
    """
    parser = argparse.ArgumentParser(description = "Run the batch.")

    parser.add_argument("instance_ids", nargs="*", help = "Pass the SWE-bench instance ids such as: sympy__sympy-15599")
    parser.add_argument("--instances-file", type=Path, help = "Pass the instances file for the batch run")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Pass the yaml config path for the agent such as: default.yaml")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Provide the directory path where run outputs will be placed")
    parser.add_argument("--score-each", action="store_true")
    parser.add_argument("--remove-images", action="store_true")
    parser.add_argument("--resume", type=Path, default=None)

    args = parser.parse_args()

    if args.remove_images and not args.score_each:
        parser.error("score-each must be given if remove-images is given.")

    if args.resume and (args.instance_ids or args.instances_file):
        parser.error("Do not give instance ids or instances_file, the ids from run.json")

    elif not args.resume and bool(args.instance_ids) == bool(args.instances_file):
        parser.error("Give either the instance ids or the instances_file.")

    config = load_config(args.config)
    if args.resume:

        run_dir = args.resume
        instance_ids = load_resume_ids(run_dir, config)

        append_resume(run_dir)
        done = finished_ids(run_dir)

        print(f"Completed id count: {len(done)}, Incomplete id count: {len(instance_ids) - len(done)}")

    else:

        if args.instances_file is None:
            instance_ids = args.instance_ids

        else:
            instance_ids = read_instances_file(args.instances_file)

        validate_instance_ids(instance_ids, config["dataset"]["name"], config["dataset"]["split"])

        run_dir = make_run_dir(args.output_dir)
        save_run_metadata(run_dir, config, instance_ids)
        done = set()

    run_tally = Counter()
    last_image = None
    scoring_failure = 0
    removal_failure = 0
    consecutive_errors = []
    stopped = False

    if args.score_each and (run_dir / "preds.jsonl").exists() and not score_new_predictions(run_dir, config):
        scoring_failure += 1

    if args.remove_images:
        for instance_id in done:
            image = swebench_image_name(instance_id)

            if image_exists(image) is True:
                if not remove_image(image):
                    removal_failure += 1

    for index, instance_id in enumerate(instance_ids, start = 1):

        if instance_id in done:
            continue

        print(f"Progress check: [{index}/{len(instance_ids)}] {instance_id}")

        new_path = keep_previous_attempt(run_dir, instance_id)

        if new_path is not None:
            print(f"Updated Path from previous attempt: {new_path}")

        record = run_and_save(run_dir, instance_id, config)
        run_tally[record["exit_status"]] += 1
        image = swebench_image_name(instance_id)

        if record["exit_status"] == "Error":
            consecutive_errors.append(record["error"])

        else:
            consecutive_errors = []

        if args.score_each and not score_new_predictions(run_dir, config):
            scoring_failure += 1

        # A refused pull leaves no image, so removing it would count a false failure.
        if args.remove_images and last_image is not None:
            if image_exists(last_image) is True:
                if not remove_image(last_image):
                    removal_failure += 1

        last_image = image

        # Checked last, so this instance is already scored and the post-loop
        # removal handles the last image the same way as a normal end.
        if len(consecutive_errors) == MAX_CONSECUTIVE_ERRORS:
            stopped = True
            break

    if args.remove_images and last_image is not None:
        if image_exists(last_image) is True:
            if not remove_image(last_image):
                removal_failure += 1

    status_list = []
    for status, count in run_tally.items():
        status_list.append(f"{status} {count}")

    status_string = ", ".join(status_list)

    tally_parts = [
        f"This session - Tally: {status_string}",
        f"Scoring failures: {scoring_failure}",
        f"Removal failures: {removal_failure}",
        f"Run Directory: {run_dir}"
    ]
    if stopped:

        for error in consecutive_errors:
            first_line = error["message"].partition("\n")[0]

            if first_line == "":
                print(f"Error type {error["type"]}, No message")

            else:
                print(f"Error type {error["type"]} because of {first_line}")

        tally_parts.append(f"Next Command: python run_batch.py --resume {run_dir} --score-each --remove-images")

    else:
        tally_parts.append(f"Next Command: python score_run.py {run_dir}")

    tally_string = ", ".join(tally_parts)
    print(tally_string)

    if stopped:
        sys.exit(1)

if __name__ == "__main__":
    main()
