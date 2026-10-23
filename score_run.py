# =============================================================================
# score_run.py
# -----------------------------------------------------------------------------
# Responsible for: Scoring one finished run folder: run the SWE-bench harness
#                  on its preds.jsonl, save scores.json, print one line per
#                  instance.
# Role in project: The scoring entry point, the counterpart to run_single.py.
#                  Run after run_single.py has written a run folder. Can be run
#                  again later on any old run folder (after deleting its
#                  scores.json and logs/).
# Assumptions:     Run from the project root (so `handling.` imports resolve).
#                  Docker running. The run folder has run.json and preds.jsonl.
#                  No API key needed: scoring never calls the LM.
# =============================================================================

import argparse
import json
from pathlib import Path

from handling.records import save_scores
from handling.scoring import collect_scores, run_harness


def load_run_dataset(run_dir: Path) -> tuple[str, str]:
    """
    Read the dataset name and split a run used, from its run.json.

    Args:
        run_dir: A run folder from records.make_run_dir().

    Returns:
        (dataset_name, split), e.g. ("SWE-bench/SWE-bench_Verified", "test").

    Why:
        run.json records the config values the run actually used, so scoring
        defaults to the same data the agent saw instead of whatever
        default.yaml says today.
    """
    # TODO 1: read run_dir / "run.json" (encoding="utf-8") with json.load.
    # TODO 2: return config["dataset"]["name"] and config["dataset"]["split"],
    #         where config is the "config" key saved by save_run_metadata.

    path = run_dir / "run.json"

    with path.open('r', encoding='utf-8') as f:
        metadata = json.load(f)

    return metadata["config"]["dataset"]["name"], metadata["config"]["dataset"]["split"]


def main() -> None:
    """
    Parse the command line, score one run folder, and print the results.

    Returns:
        None. Writes scores.json into the run folder and prints a summary.

    Why:
        Keeps scoring a separate command from running: it takes minutes of
        Docker time, needs no API calls, and must be rerunnable on an old run
        without spending anything on the agent again.
    """

    # TODO 1: argparse:
    #           run_dir                positional, type=Path (e.g. runs/2026-10-21T00-57-20)
    #           --dataset-name         default None -> taken from run.json
    #           --split                default None -> taken from run.json
    #           --max-workers          type=int, default 1
    #           --timeout              type=int, default 1800 (seconds per instance)
    #         --dataset-name exists for runs made before the Stage 7 dataset
    #         switch: their run.json says princeton-nlp/SWE-bench_Verified,
    #         which lacks the fields the harness needs. Pass
    #         --dataset-name SWE-bench/SWE-bench_Verified for those.
    # TODO 2: fill in whichever of dataset_name / split were not given, from
    #         load_run_dataset(args.run_dir). (Read run.json only once.)
    # TODO 3: report = run_harness(args.run_dir, dataset_name, split,
    #         args.max_workers, args.timeout)
    # TODO 4: scores = collect_scores(args.run_dir, report, dataset_name, split),
    #         then scores_path = save_scores(args.run_dir, scores).
    # TODO 5: print one line per instance: instance_id, status, and when the
    #         test fields are not None, passed/total for FAIL_TO_PASS and
    #         PASS_TO_PASS (e.g. "F2P 1/1, P2P 84/85"). Then the summary counts
    #         and scores_path.


    parser = argparse.ArgumentParser(description = "Score run")
    parser.add_argument("run_dir", type=Path, help="Provide a run directory, e.g runs/2026-10-21T00-57-20")
    parser.add_argument("--dataset-name", default=None, help="Pass Dataset name used for the run")
    parser.add_argument("--split", default=None, help="Pass the dataset split used for the run")
    parser.add_argument("--max-workers", type=int, default=1, help="Pass the number of workers for parallel scoring")
    parser.add_argument("--timeout", type=int, default=1800, help="Pass the duration for each instance test before timeout")

    args = parser.parse_args()

    dataset_name, split = load_run_dataset(args.run_dir)

    # Note that here we are overwriting the dataset name and split
    # if the command line provides a different datset. Only likely to occur if
    # we are testing one dataset and scoring against another.

    dataset_name = args.dataset_name if args.dataset_name is not None else dataset_name
    split = args.split if args.split is not None else split

    report = run_harness(args.run_dir, dataset_name, split, args.max_workers, args.timeout)
    scores = collect_scores(args.run_dir, report, dataset_name, split)
    score_path = save_scores(args.run_dir, scores)

    for instance_id, instance_values in scores["instances"].items():
        if instance_values["fail_to_pass"]is not None and instance_values["pass_to_pass"] is not None:

            fail_to_pass = instance_values["fail_to_pass"]
            pass_to_pass = instance_values["pass_to_pass"]

            f2p_passed = len(fail_to_pass["success"])
            f2p_total = f2p_passed + len(fail_to_pass["failure"])

            p2p_passed = len(pass_to_pass["success"])
            p2p_total = p2p_passed + len(pass_to_pass["failure"])

            print(
                f"Instance_id: {instance_id},",
                f"status: {instance_values["status"]},",
                f"F2P: {f2p_passed}/{f2p_total}, P2P: {p2p_passed}/{p2p_total}"
            )

        else:
            print(
                f"Instance_id: {instance_id}",
                f"status = {instance_values["status"]}",
            )

    status_list = []
    for status, count in scores["summary"].items():
        status_list.append(f"{status} {count}")

    status_string = ", ".join(status_list)
    print(status_string, f"score_path: {score_path}")



if __name__ == "__main__":
    main()
