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
# Deferred:        --resume (skip ids already in preds.jsonl) and removing each
#                  instance image after its run (each is several GB).
# =============================================================================

import argparse
from collections import Counter
from pathlib import Path

from datasets import load_dataset

from handling.records import make_run_dir, save_run_metadata
from run_single import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_DIR,
    load_config,
    run_and_save,
)


def read_instances_file(path: Path) -> list[str]:
    """
    Read instance ids from a text file, one id per line.

    Args:
        path: A UTF-8 text file. Blank lines are skipped.

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
            if line:
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

    args = parser.parse_args()

    if bool(args.instance_ids) == bool(args.instances_file):
        parser.error("Give either the instance ids or the instances_file.")

    if args.instances_file is None:
        instance_ids = args.instance_ids

    else:
        instance_ids = read_instances_file(args.instances_file)

    config = load_config(args.config)
    validate_instance_ids(instance_ids, config["dataset"]["name"], config["dataset"]["split"])

    run_dir = make_run_dir(args.output_dir)
    save_run_metadata(run_dir, config, instance_ids)
    run_tally = Counter()

    for index, instance_id in enumerate(instance_ids, start = 1):
        print(f"Progress check: [{index}/{len(instance_ids)}] {instance_id}")
        record = run_and_save(run_dir, instance_id, config)
        run_tally[record["exit_status"]] += 1


    status_list = []
    for status, count in run_tally.items():
        status_list.append(f"{status} {count}")

    status_string = ", ".join(status_list)

    print(f"Tally: {status_string}, Run Directory: {run_dir}, Next: python score_run.py {run_dir}")


if __name__ == "__main__":
    main()
