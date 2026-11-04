# =============================================================================
# merge_scores.py
# -----------------------------------------------------------------------------
# Responsible for: Combining the scores.json files of shard run folders into
#                  one result, after checking that the shards really form one
#                  run.
# Role in project: Stage 12a, the last step of a sharded run. Each shard was
#                  run by run_batch.py from a shard_instances.py file and
#                  scored (score_run.py writes scores.json). The merged file
#                  is what results are reported from.
# Assumptions:     Run from the project root. Every shard folder has run.json
#                  and scores.json. Needs no Docker and no API key.
# =============================================================================

import argparse
import json
from collections import Counter
from pathlib import Path

from handling.scoring import STATUSES
from run_batch import read_instances_file


def load_shard(run_dir: Path) -> dict:
    """
    Read one shard's run.json and scores.json.

    Args:
        run_dir: A shard's run folder.

    Returns:
        {"run_dir": Path, "run": the run.json dict, "scores": the scores.json dict}.

    Raises:
        FileNotFoundError: if either file is missing (for example the shard
            was never scored).

    Why:
        One place reads a shard, so the checks and the merge both work on the
        same dicts and never read the files twice.
    """
    run_path = run_dir / "run.json"
    score_path = run_dir / "scores.json"

    if not run_path.exists():
        raise FileNotFoundError(f"Shard was never ran and no run.json file exists: {run_path}")

    if not score_path.exists():
        raise FileNotFoundError(f"Shard was not scored and no scores.json file exists: {score_path}")

    with run_path.open('r', encoding='utf-8') as f:
        run_metadata = json.load(f)

    with score_path.open('r', encoding='utf-8') as f:
        score_metadata = json.load(f)

    return {"run_dir": run_dir, "run": run_metadata, "scores": score_metadata}

def check_shards(shards: list[dict], expected_ids: list[str]) -> None:
    """
    Refuse to merge shards that are not one complete, consistent run.

    Args:
        shards: Results of load_shard, one per shard folder.
        expected_ids: The ids of the original (unsharded) ids file.

    Returns:
        None.

    Raises:
        ValueError: listing every problem found, not only the first:
            - a run.json "config" that differs from the first shard's
            - a scores.json "dataset", "split" or "swebench_version" that
              differs from the first shard's
            - an id in more than one shard's run.json "instance_ids"
            - ids in expected_ids that no shard has, or shard ids not in it
            - a shard whose scores.json "instances" does not cover all of its
              run.json "instance_ids" (stopped or not fully scored)

    Why:
        A merged score is only meaningful if every shard ran the same config,
        was scored by the same harness version, and together they cover the
        ids exactly once. Mixing would change the number with nothing to show
        it, the same reason load_resume_ids refuses a different config.
        Listing every problem at once means one fix-and-rerun, not one per
        problem.
    """
    problems = []

    for shard in shards[1:]:
        for key in ("dataset", "split", "swebench_version"):

            if shard["scores"][key] != shards[0]["scores"][key]:
                problems.append(f"{key} value does not match: {shard['run_dir']} {key} is {shard['scores'][key]}, first shard {shards[0]['run_dir']} is {shards[0]['scores'][key]}")

        if shard["run"]["config"] != shards[0]["run"]["config"]:
            problems.append(f"Mismatch in the config at {shard['run_dir']}")


    id_counts = Counter()
    duplicate_ids = []

    for shard in shards:
        id_counts.update(shard["run"]["instance_ids"])

    for instance_id, count in id_counts.items():
        if count > 1:
            duplicate_ids.append(instance_id)

    if duplicate_ids != []:
        problems.append(f"Duplicate ids found in the shards: {duplicate_ids}")

    shard_ids = set(id_counts)
    expected = set(expected_ids)
    missing = expected - shard_ids
    extra = shard_ids - expected

    if len(missing) > 0:
        problems.append(f"Expected ids are missing from the shards: {missing}")

    if len(extra) > 0:
        problems.append(f"There are too many ids in the shards: {extra}")

    for shard in shards:
        missing_ids = []

        for instance_id in shard["run"]["instance_ids"]:
            if instance_id not in shard["scores"]["instances"]:
                missing_ids.append(instance_id)

        if missing_ids != []:
            problems.append(f"There are {len(missing_ids)} missing from scores.json in {shard['run_dir']}, instance must be re-scored.")

    if problems != []:

        problems_line = "\n".join(problems)
        raise ValueError(f"{len(problems)} problems occurred:\n{problems_line}")


def merge_shard_scores(shards: list[dict]) -> dict:
    """
    Combine checked shards into one scores dict.

    Args:
        shards: Results of load_shard that passed check_shards.

    Returns:
        {"run_ids": [each shard's run_id], "dataset": str, "split": str,
         "swebench_version": str,
         "summary": {status: count for every status in STATUSES},
         "instances": {instance_id: the shard's per-instance entry}}.

    Why:
        The same shape as one scores.json, with run_ids in place of run_id,
        so anything that reads summary and instances works on either. The
        summary is recounted from the instances and not added up from the
        shards' summaries, so it can never disagree with them.
    """
    merged_scores = {}

    for shard in shards:
        merged_scores.update(shard["scores"]["instances"])

    summary = {status: 0 for status in STATUSES}

    for entry in merged_scores.values():
        summary[entry["status"]] += 1

    run_ids = []
    for shard in shards:

        run_ids.append(shard["scores"]["run_id"])

    return_dict = {
        "run_ids": run_ids,
        "dataset": shards[0]["scores"]["dataset"],
        "split": shards[0]["scores"]["split"],
        "swebench_version": shards[0]["scores"]["swebench_version"],
        "summary": summary,
        "instances": merged_scores
    }

    return return_dict

def main() -> None:
    """
    Parse the command line, check the shards, merge them, and write the result.

    Returns:
        None. Writes the merged JSON to --output and prints the summary.

    Why:
        The output path is required and not inside any shard folder, because
        the merged result belongs to none of them. It is opened with mode
        "x" like save_scores, so an earlier merge is never overwritten.
    """
    parser = argparse.ArgumentParser(description="Merge shard scores")
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--instances-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()

    shards = []

    for run_dir in args.run_dirs:
        shards.append(load_shard(run_dir))

    expected_ids = read_instances_file(args.instances_file)
    check_shards(shards, expected_ids)

    merged_scores = merge_shard_scores(shards)

    args.output.parent.mkdir(parents=True, exist_ok=True)

    with open(args.output, "x", encoding="utf-8") as f:
        json.dump(merged_scores, f, indent=2, ensure_ascii=False)

    for status, count in merged_scores["summary"].items():
        print(f"Status: {status}, Count: {count}")

    print(f"Output Path: {args.output}")

if __name__ == "__main__":
    main()
