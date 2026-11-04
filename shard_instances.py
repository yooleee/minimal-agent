# =============================================================================
# shard_instances.py
# -----------------------------------------------------------------------------
# Responsible for: Splitting an existing ids file into N shard files that can
#                  run at the same time, without splitting any Docker
#                  environment (repo, version) across shards.
# Role in project: Stage 12a. Each shard file is run by its own run_batch.py
#                  process into its own run folder. merge_scores.py then
#                  combines the shards' scores.json files into one result.
# Assumptions:     Run from the project root. The ids file was written by
#                  sample_instances.py (read with read_instances_file). The
#                  dataset is read from the config, as in sample_instances.py.
# =============================================================================

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from multiprocessing import Value
from pathlib import Path

from datasets import load_dataset

from run_batch import read_instances_file
from run_single import DEFAULT_CONFIG_PATH, load_config
from sample_instances import order_by_environment, print_repo_counts, write_ids_file


def shard_ids(
    ids: list[str], environments: dict[str, tuple[str, str]], n_shards: int
) -> list[list[str]]:
    """
    Split ids into n_shards lists of whole environment groups, about equal in size.

    Args:
        ids: The ids to split (e.g. the 360 held-out ids).
        environments: {instance_id: (repo, version)} for every id in ids.
        n_shards: How many shards to make. At least 1.

    Returns:
        n_shards lists. Together they hold every id in ids exactly once, and
        each list is in (repo, version, instance_id) order.

    Raises:
        ValueError: if n_shards < 1, if an id has no entry in environments, or
            if there are fewer environment groups than shards (a shard would
            be empty).

    Why:
        Ids that share an environment must stay in one shard. Within a shard,
        9b's "remove the previous image" keeps shared layers between
        neighbours. Across shards, one process could remove an image another
        is about to reuse. Groups are assigned largest first, each to the
        currently smallest shard (greedy LPT, "longest processing time
        first"). On all_500 and heldout_360 this gives exactly equal counts
        for 2 to 5 shards. Equal counts are not equal times, since repos
        score at different speeds. Pure function, so it can be tested
        without the dataset.
    """
    if n_shards < 1:
        raise ValueError(f"Number of shards below minimum required: {n_shards}")

    missing_ids = []
    id_env_grouping = defaultdict(list)

    for instance_id in ids:
        if instance_id in environments:
            id_env_grouping[environments[instance_id]].append(instance_id)

        else:
            missing_ids.append(instance_id)

    if missing_ids != []:
        raise ValueError(f"Ids missing from environment: {missing_ids}")

    if len(id_env_grouping) < n_shards:
        raise ValueError(f"Number of id groups {len(id_env_grouping)} is less than number of shards {n_shards}")

    id_env_grouping = sorted(id_env_grouping.items(), key = lambda item:(-len(item[1]), item[0]))
    shards = [[] for _ in range(n_shards)]

    for env_key, group_ids in id_env_grouping:
        smallest_index = 0

        for k in range(1, n_shards):
            if len(shards[k]) < len(shards[smallest_index]):
                smallest_index = k

        shards[smallest_index].extend(group_ids)

    ordered_shards = []
    for shard in shards:
        ordered_shards.append(order_by_environment(shard, environments))

    return ordered_shards


def main() -> None:
    """
    Parse the command line, split the ids file, and write one file per shard.

    Returns:
        None. Writes <stem>.shard-<k>-of-<N>.txt next to the ids file and
        prints each path with its per-repo counts.

    Why:
        A separate script from sample_instances.py, because sharding splits a
        file that already exists and draws no sample. Writing the shards next
        to their source keeps the pair together in instances/.
    """
    parser = argparse.ArgumentParser(description="Split an ids file into shards")
    parser.add_argument("--instances-file", type=Path, required=True)
    parser.add_argument("--shards", type=int, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)

    args = parser.parse_args()


    instance_ids = read_instances_file(args.instances_file)
    config = load_config(args.config)
    dataset = load_dataset(config["dataset"]["name"], split=config["dataset"]["split"])
    environments = {}
    for row in dataset:
        environments[row["instance_id"]] = (row["repo"], row["version"])



    shard_ids_list = shard_ids(instance_ids, environments, args.shards)
    created = datetime.now(timezone.utc).isoformat()
    n_shards = args.shards

    for k, shard in enumerate(shard_ids_list, start=1):

        shard_path = args.instances_file.with_name(f"{args.instances_file.stem}.shard-{k}-of-{n_shards}.txt")

        header_lines = [
        f"source={args.instances_file}",
        f"shard={k}/{n_shards}",
        f"n={len(shard)}",
        f"dataset={config["dataset"]["name"]}",
        f"split={config["dataset"]["split"]}",
        f"created={created}",
        "order=repo,version"
        ]

        write_ids_file(shard_path, shard, header_lines)
        print_repo_counts(shard, shard_path)

if __name__ == "__main__":
    main()
