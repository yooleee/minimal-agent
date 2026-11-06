# =============================================================================
# narration_measure.py
# -----------------------------------------------------------------------------
# Responsible for: Counting, from saved trajectories, how much of a run was
#                  lost to the narration problem (Step 6b): replies with other
#                  than one bash-action block, rejected replies, and empty
#                  patches whose edits were only in rejected replies.
# Role in project: Stage 10a. Run on the dev_25 runs and the pool part of the
#                  Stage 9 run before any fix exists, so arm 2's primary
#                  metric is set first. Stage 10c and Stage 12 use it again.
# Assumptions:     Run from the project root. Needs no Docker and no API key.
#                  Trajectories are <run_dir>/<instance_id>.traj.json. Attempt
#                  files from a resume (<id>.attempt-<n>.json) are not read.
#                  Held-out ids are refused unless --allow-heldout is given,
#                  because they are not read before Stage 12 (Step 9a).
# =============================================================================

import argparse
import json
import re
from pathlib import Path

from class_agent import ACTION_PATTERN
from run_batch import read_instances_file

HELDOUT_FILE = Path("instances/heldout_360_seed1001.txt")

# How the agent's reply to each model reply begins (class_agent.py). These two
# mean the reply was rejected and nothing in it ran.
FORMAT_ERROR_PREFIX = "Found "
PREMATURE_EXIT_PREFIX = "Nothing has been executed yet"
EMPTY_EXIT_PREFIX = "Exit refused"
# These mean the reply was acted on.
ACCEPTED_PREFIXES = ("Exit code:", "Command timed out", "LM requested to quit", "Step limit reached")

# A heuristic for "this block changes a file": in-place tools, Python file
# writes, and shell redirects to a filename outside /dev/null and /tmp. The
# redirect needs a name with an extension and no "-", "=", "<" or ">" before it,
# or it matches Python in a heredoc such as "-> None". It can miss an unusual
# edit, and it can count a write to a scratch file. So a count built on it is an
# estimate, and each empty patch it flags is checked by reading the trajectory.
EDIT_PATTERN = re.compile(
    r"sed\s+-i|perl\s+-[a-z]*i|\bgit\s+apply\b|\bpatch\s+-|\btee\s|write_text\("
    r"|open\([^)]*['\"][wa]b?\+?['\"]"
    r"|(?<![-0-9&=<>])>>?\s*(?!/dev/null|&|/tmp)[\w./-]*[\w-]\.\w+"
)

# Int fields counted in the trajectory measurement functin. Required again when
# measuring the run to confirm the results of each instance trajectory.
SUM_FIELDS = ("steps", "not_one_block", "zero_blocks", "multi_blocks",
                "format_errors", "premature_exits", "empty_exits", "rejected")

# Run fields counted in the run measurements.


RUN_FIELDS = ("runs_with_rejection", "runs_with_rejected_edit",
    "empty_patches", "empty_with_rejected_edit")


def classify_response(content: str) -> str:
    """
    Name what happened to a model reply, from the message that answered it.

    Args:
        content: The content of the user message right after a model reply.

    Returns:
        "format_error", "premature_exit", "empty_exit" or "accepted".

    Raises:
        ValueError: if the message starts with none of the known prefixes.

    Why:
        The answering message is what the agent really did, so nothing here
        has to re-derive the agent's rules. An unknown message raises instead
        of counting as accepted, because a silent "accepted" would undercount
        rejections with nothing to show it. A new rejection in the agent (Step
        10b added the refused exit) needs its prefix here.
    """
    if content.startswith(FORMAT_ERROR_PREFIX):
        return "format_error"

    elif content.startswith(PREMATURE_EXIT_PREFIX):
        return "premature_exit"

    elif content.startswith(EMPTY_EXIT_PREFIX):
        return "empty_exit"

    elif content.startswith(ACCEPTED_PREFIXES):
        return "accepted"

    else:
        raise ValueError(f"Unknown error occurred in the session: {content[:60]}")

def measure_trajectory(trajectory: dict) -> dict:
    """
    Count one trajectory's narration events.

    Args:
        trajectory: A loaded <instance_id>.traj.json.

    Returns:
        {"steps": int, "not_one_block": int, "zero_blocks": int,
         "multi_blocks": int, "format_errors": int,
         "premature_exits": int, "empty_exits": int,
         "rejected": int (format_errors + premature_exits + empty_exits),
         "rejected_edit": bool (a block that did not run held an edit),
         "empty_patch": bool,
         "empty_with_rejected_edit": bool (both of the above)}.

    Why:
        Blocks are counted from the reply text and rejections from the answer,
        as two separate facts. Today they match, since every reply with other
        than one block is rejected. Step 10b runs the first block of a
        multi-block reply, so format_errors will fall by design while
        not_one_block shows whether the model itself changed. Measuring only
        rejections would hide that. An empty patch needs a finished run, so a
        crashed run ("Error") is never counted as one: its model_patch is
        partial or missing for another reason.
    """
    steps = trajectory["step_count"]
    not_one_block = 0
    zero_blocks = 0
    multi_blocks = 0
    format_errors = 0
    premature_exits = 0
    empty_exits = 0
    rejected_edit = False


    for index, message in enumerate(trajectory["messages"]):
        blocks = re.findall(ACTION_PATTERN, message["content"], re.DOTALL)

        if message["role"] == "assistant":
            if len(blocks) != 1:
                not_one_block += 1

            if len(blocks) == 0:
                zero_blocks += 1

            elif len(blocks) >= 2:
                multi_blocks += 1

            if index + 1 < len(trajectory["messages"]):
                output = classify_response(trajectory["messages"][index+1]["content"])

                if output == "format_error":
                    format_errors += 1

                    for block in blocks:
                        if EDIT_PATTERN.search(block):
                            rejected_edit = True

                elif output == "accepted" and len(blocks) >= 2:
                    for block in blocks[1:]:
                        if EDIT_PATTERN.search(block):
                            rejected_edit = True

                elif output == "empty_exit":
                    empty_exits += 1

                elif output == "premature_exit":
                    premature_exits += 1

    if ((trajectory["model_patch"] == "") or (trajectory["model_patch"] is  None)) and trajectory["exit_status"] != "Error":
        empty_patch = True

    else:
        empty_patch = False

    return {
        "steps": steps, "not_one_block": not_one_block,
        "zero_blocks": zero_blocks, "multi_blocks": multi_blocks,
        "format_errors": format_errors, "premature_exits": premature_exits,
        "empty_exits": empty_exits, "rejected": format_errors + premature_exits + empty_exits,
        "rejected_edit": rejected_edit, "empty_patch": empty_patch,
        "empty_with_rejected_edit": empty_patch and rejected_edit
    }

def measure_run(run_dir: Path, instance_ids: list[str]) -> dict:
    """
    Measure the given ids of one run folder and add up the counts.

    Args:
        run_dir: A run folder.
        instance_ids: The ids to read. Others in the folder are not opened.

    Returns:
        {"run_dir": str, "n": int, "steps": int, "not_one_block": int,
         "zero_blocks": int, "multi_blocks": int, "format_errors": int,
         "premature_exits": int, "empty_exits": int, "rejected": int,
         "runs_with_rejection": int, "runs_with_rejected_edit": int,
         "empty_patches": int, "empty_with_rejected_edit": int,
         "lost_share": float (rejected / steps), "instances":
         {instance_id: the measure_trajectory dict}}.

    Raises:
        FileNotFoundError: if an id has no trajectory in run_dir.

    Why:
        Opening only the requested ids, and not globbing the folder, is what
        keeps a pool measurement from reading held-out trajectories. A missing
        id raises because a total over fewer instances would look complete.
        lost_share is the primary metric for arm 2: the share of the steps a
        run paid for in which nothing ran.
    """
    totals = {field: 0 for field in SUM_FIELDS}
    instances = {}

    runs_with_rejection = 0
    runs_with_rejected_edit = 0
    empty_patches = 0
    empty_with_rejected_edit = 0

    for instance_id in instance_ids:
        id_path = run_dir / f"{instance_id}.traj.json"

        with open(id_path, encoding="utf-8") as f:
            loaded_id = json.load(f)

        measured_trajectory = measure_trajectory(loaded_id)
        instances[instance_id] = measured_trajectory

        for field in SUM_FIELDS:
            totals[field] += measured_trajectory[field]

        if measured_trajectory["rejected"] > 0:
            runs_with_rejection += 1

        if measured_trajectory["rejected_edit"]:
            runs_with_rejected_edit += 1

        if measured_trajectory["empty_patch"]:
            empty_patches += 1

        if measured_trajectory["empty_with_rejected_edit"]:
            empty_with_rejected_edit += 1

    lost_share = totals["rejected"] / totals["steps"]

    return {
        "run_dir": str(run_dir), "n": len(instance_ids), **totals,
        "runs_with_rejection": runs_with_rejection, "runs_with_rejected_edit": runs_with_rejected_edit,
        "empty_patches": empty_patches, "empty_with_rejected_edit": empty_with_rejected_edit,
        "lost_share": lost_share, "instances": instances
    }

def main() -> None:
    """
    Parse the command line, measure each run, and print one summary per run.

    Returns:
        None. Prints the totals. Writes JSON if --output is given.

    Why:
        --instances-file is required so the ids are always a deliberate
        choice. Comparing the ids with the held-out file before opening any
        trajectory enforces the Step 9a boundary in code. --allow-heldout is
        there for Stage 12, where the rules of Step 12b report this measure.
    """
    parser = argparse.ArgumentParser(description="Measure narration in saved runs")
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--instances-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--allow-heldout", action="store_true")

    args = parser.parse_args()

    instance_ids = read_instances_file(args.instances_file)

    if not args.allow_heldout:
        heldout_ids = set(read_instances_file(HELDOUT_FILE))
        overlap_count = len(set(instance_ids) & heldout_ids)

        if overlap_count > 0:
            raise ValueError(f"There are {overlap_count} overlapping ids in the held-out set.")


    results = []

    for run_dir in args.run_dirs:
        result = measure_run(run_dir, instance_ids)
        results.append(result)

        parts = [f"{result['run_dir']}", f"n {result['n']}"]

        for field in SUM_FIELDS:
            parts.append(f"{field} {result[field]}")

        for field in RUN_FIELDS:
            parts.append(f"{field} {result[field]}")

        parts.append(f"lost_share {result['lost_share']:.1%}")

        print(", ".join(parts))

        flagged = []

        for instance_id, measured in result["instances"].items():
            if measured["empty_with_rejected_edit"]:
                flagged.append(instance_id)

        if flagged != []:
            print(f"Check by hand: {flagged}")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)

        with open(args.output, "x", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    main()
