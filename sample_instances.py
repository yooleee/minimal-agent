# =============================================================================
# sample_instances.py
# -----------------------------------------------------------------------------
# Responsible for: Drawing a seeded random sample of instance ids from the
#                  configured dataset and writing it to an ids file that
#                  run_batch.py --instances-file reads.
# Role in project: Builds the Stage 8d dev set (25 SWE-bench Verified ids).
#                  Run once; the written file is tracked and is what every
#                  later batch on this set uses.
# Assumptions:     Run from the project root. The dataset is read from the
#                  config, so the sample comes from the same data the batch
#                  validates against.
# =============================================================================

import argparse
import random
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from datasets import load_dataset

from run_single import DEFAULT_CONFIG_PATH, load_config


def sample_ids(all_ids: list[str], n: int, seed: int, exclude: list[str]) -> list[str]:
    """
    Pick n ids at random, reproducibly, leaving out the excluded ones.

    Args:
        all_ids: Every instance id in the dataset split.
        n: How many to pick.
        seed: Seed for the random generator.
        exclude: Ids that must not be picked (e.g. sympy__sympy-15599, which
            already has 13 runs and would not tell us anything new).

    Returns:
        The n chosen ids, sorted alphabetically.

    Raises:
        ValueError: if n is larger than the number of ids left after exclusion,
            or an excluded id is not in all_ids (most likely a typo, which
            would otherwise silently exclude nothing).

    Why:
        Pure function (no I/O), so it can be tested without the dataset.
        Sorting the candidates BEFORE sampling makes the result depend only on
        the set of ids and the seed, not on the dataset's row order.
        random.Random(seed) is a private generator: nothing else in the
        process can advance it, so the same inputs give the same sample.
        The output is sorted so the ids file is easy to read and diff.
    """
    unknown_ids = []
    candidates = []

    for instance_id in exclude:
        if instance_id not in all_ids:
            unknown_ids.append(instance_id)

    if unknown_ids != []:
        raise ValueError(f"Excluded ids not in the dataset: {unknown_ids}")

    for instance_id in sorted(all_ids):
        if instance_id not in exclude:
            candidates.append(instance_id)

    if n > len(candidates):
        raise ValueError(f"The selected instance count is greater than the number of available candidates {len(candidates)}")

    rng = random.Random(seed)
    chosen_ids = sorted(rng.sample(candidates, n))

    return chosen_ids

def write_ids_file(path: Path, ids: list[str], header_lines: list[str]) -> None:
    """
    Write the ids, one per line, under a block of # comment lines.

    Args:
        path: Where to write, e.g. instances/dev_25_seed<seed>.txt.
        ids: The ids to write.
        header_lines: Provenance, one fact per line (seed, dataset, split, n,
            excluded ids, UTC date). Each is written as "# <line>".

    Returns:
        None.

    Raises:
        FileExistsError: if the file already exists.

    Why:
        The file carries its own provenance, so the sample can be traced and
        redrawn without this conversation. The ids themselves are the source
        of truth: Python does not promise random.sample gives the same result
        across versions, so the seed alone is a record, not a guarantee.
        Mode "x" follows the project rule that nothing is silently overwritten.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("x", encoding="utf-8") as f:

        for line in header_lines:
            f.write("# " + line + "\n")

        for instance_id in ids:
            f.write(instance_id + "\n")


def main() -> None:
    """
    Parse the command line, sample the ids, and write the ids file.

    Returns:
        None. Writes the file and prints its path and the per-repo counts.

    Why:
        Printing the per-repo counts lets you see at a glance whether the
        sample looks like Verified (about half django) before spending
        anything on it.
    """
    parser = argparse.ArgumentParser(description = "Sample instance ids")
    parser.add_argument("--n", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--exclude", nargs="*", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    args = parser.parse_args()
    config = load_config(args.config)
    dataset =  load_dataset(config["dataset"]["name"], split=config["dataset"]["split"])

    all_ids = list(dataset["instance_id"])
    ids = sample_ids(all_ids, args.n, args.seed, args.exclude)

    header_lines = [
        f"seed={args.seed}",
        f"dataset={config["dataset"]["name"]}",
        f"split={config["dataset"]["split"]}",
        f"n={args.n}", f"excluded={args.exclude}",
        f"created={datetime.now(timezone.utc).isoformat()}"
    ]

    write_ids_file(args.output, ids, header_lines)


    #print(args.output)
    repo_prefix = []
    run_count = []

    for instance_id in ids:
        repo_prefix.append(instance_id.rsplit("__", 1)[0])

    for owner, count in Counter(repo_prefix).items():
        run_count.append(f"{owner} {count}")

    run_count_string = ", ".join(run_count)
    print(f"Per Repo: {run_count_string}", f"Path: {args.output}")

if __name__ == "__main__":
    main()
