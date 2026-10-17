# =============================================================================
# run_single.py
# -----------------------------------------------------------------------------
# Responsible for: Running the agent on ONE SWE-bench instance end to end:
#                  load the instance, build the prompt, build the environment
#                  and agent, run, and always clean up the container.
# Role in project: The entry point (replaces get_instance.py, problem.txt and
#                  the __main__ block in class_agent.py). Later stages call
#                  run_instance() once per instance for batch runs.
# Assumptions:     Run from the project root (so `config.` and `handling.`
#                  imports resolve). OPENAI_API_KEY in .env. Docker running.
#                  Instance ids use SWE-bench's double underscore form.
# =============================================================================

import argparse
from pathlib import Path

import yaml
from datasets import load_dataset

from class_agent import Agent
from config.environments import DockerEnvironment, swebench_image_name

# TODO (Step 4c): once the YAML holds these values, delete DATASET_NAME, DATASET_SPLIT,
#   CONTAINER_ENV and TASK_TEMPLATE below, and drop `system_prompt` from the
#   class_agent import. Replace them with one constant:
#     DEFAULT_CONFIG_PATH = Path(__file__).parent / "config" / "default.yaml"
#   Path(__file__).parent is this file's folder, so the default works no matter which
#   directory you launch from, and pathlib joins with the right separator on
#   Windows/WSL/macOS.

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config" / "default.yaml"


def load_config(path: Path) -> dict:
    """
    Read the YAML run configuration into a dict.

    Args:
        path: Path to a YAML file shaped like config/default.yaml.

    Returns:
        The parsed config: a dict with "dataset", "model", "agent", "environment".

    Raises:
        TypeError: if the file doesn't parse to a mapping (e.g. it's empty).

    Why:
        One place turns the file into data. Everything downstream receives plain
        values (dependency injection again), so Agent and the environments never
        know a YAML file exists, and tests can pass values directly.
        Lives here because run_single.py is the only consumer. Move it to a shared
        module when the Stage 8 batch runner needs it too.
    """
    # TODO 1: open path with encoding="utf-8" (explicit, so Windows doesn't fall back
    #         to its locale encoding) and parse it with yaml.safe_load (never yaml.load)
    # TODO 2: safe_load returns None for an empty file and a list for a file that's a
    #         list. If the result isn't a dict, raise ValueError naming the path
    # TODO 3: return it. Don't check every key here: a missing key raises KeyError
    #         where it's read, which happens before the container starts (see 4c)

    config_path = Path(path)

    with config_path.open("r", encoding="utf-8") as file:
        yaml_config = yaml.safe_load(file)

        if not isinstance(yaml_config, dict):
            raise TypeError(f"{config_path} is not a dict, must be a dict for the config to load.")

        return yaml_config



# TODO (Step 4c): take the dataset name and split as parameters
#   (instance_id, dataset_name, split) instead of reading the module constants, and use
#   them in the ValueError message. The caller passes config["dataset"]["name"] / ["split"].
def load_instance(instance_id: str, dataset_name: str, split: str) -> dict:
    """
    Load one SWE-bench Verified instance by its id.

    Args:
        instance_id: e.g. "sympy__sympy-15599".
        dataset_name: e.g. "princeton-nlp/SWE-bench_Verified"
        split

    Returns:
        The dataset row as a dict (keys include "instance_id", "repo",
        "problem_statement", "FAIL_TO_PASS", "PASS_TO_PASS").

    Raises:
        ValueError: if no row has that instance_id.

    Why:
        Replaces get_instance.py + problem.txt: the instance is read fresh at run
        time, so there is no intermediate file to go stale between runs.
    """
    # TODO 1: ds = load_dataset(DATASET_NAME, split=DATASET_SPLIT)
    #         (first call downloads and caches; later calls read the local cache)
    # TODO 2: find the row whose "instance_id" equals instance_id
    #         (your get_instance.py used next(...) over a generator; note that
    #         next() with no default raises StopIteration when nothing matches)
    # TODO 3: if no match, raise ValueError naming the id, so a typo (like the
    #         single-underscore one in Step 2) fails here with a clear message
    #         instead of as a confusing Docker pull error later
    # TODO 4: return the row

    dataset = load_dataset(dataset_name, split = split)

    try:
        instance = next(x for x in dataset if x["instance_id"] == instance_id)

    except StopIteration:
        raise ValueError(f"No instance {instance_id!r} in {dataset_name} ({split})") from None

    return instance


# TODO (Step 4c): signature becomes build_messages(instance, config).
#   - system = config["agent"]["system_template"].format(
#         timeout=config["environment"]["timeout"],      <- the prompt can no longer
#         output_limit=..., head_chars=..., tail_chars=...)   disagree with the env
#   - task = config["agent"]["instance_template"].format(repo=..., problem_statement=...)
#   A misspelled placeholder raises KeyError here, before Docker starts.
def build_messages(instance: dict, config: dict) -> list[dict]:
    """
    Build the initial chat history for one instance.

    Args:
        instance: A row from load_instance().
        Config: config providing the system and task templates for the agent.

    Returns:
        [system message, user task message], the shape Agent.__init__ validates.

    Why:
        Keeps prompt assembly separate from running, so you can print and
        inspect the exact prompt for an instance without starting Docker.
    """
    # TODO 1: task = TASK_TEMPLATE.format(repo=..., problem_statement=...)
    #         using the instance's "repo" and "problem_statement" fields
    # TODO 2: return [{"role": "system", "content": system_prompt},
    #                 {"role": "user", "content": task}]

    system = config["system_template"].format(
        timeout = config["environment"]["timeout"],
        output_limit = config["agent"]["output_limit"],
        head_chars = config["agent"]["head_chars"],
        tail_chars = config["agent"]["tail_chars"]
    )

    task = config["task_template"].format(
        repo = instance["repo"],
        problem_statement = instance["problem_statement"]
    )

    return [{"role": "system", "content": system}, {"role": "user", "content": task}]


# TODO (Step 4c): signature becomes run_instance(instance_id, config).
#   - load_instance / build_messages get the config values they now take
#   - DockerEnvironment(swebench_image_name(instance_id), cwd=..., timeout=..., env=...)
#     from config["environment"]
#   - Agent(messages, env, model=config["model"]["name"], max_steps=...,
#     max_format_errors=..., output_limit=..., head_chars=..., tail_chars=...)
#     Pass each value by keyword. `**config["agent"]` would be shorter, but the agent
#     section also holds the templates, which Agent doesn't accept, and explicit
#     keywords show exactly what Agent receives.
#   - Build the Agent BEFORE env.start() if you can: its __init__ now validates
#     head/tail limits, so a bad config fails before a container exists.
#   Seam (Stage 6): the run record will want the config it ran with, for
#   reproducibility. Having `config` in scope here is what makes that a one-line addition.
def run_instance(instance_id: str, config: dict) -> dict:
    """
    Run the agent on one instance in a fresh container and return a result record.

    Args:
        instance_id: SWE-bench instance id.
        config: config providing the dataset information, model name and environment variables.

    Returns:
        {"instance_id": str, "exit_status": str, "model_patch": str | None}.
        model_patch is None until Stage 5 implements patch capture.

    Why:
        One function = one evaluation unit. Batch runs (Stage 8) will call this
        in a loop; scoring (Stage 7) will consume the records it returns.
    """
    # TODO 1: instance = load_instance(instance_id)
    # TODO 2: messages = build_messages(instance)
    # TODO 3: env = DockerEnvironment(swebench_image_name(instance_id), env=CONTAINER_ENV)
    # TODO 4: model_patch = None
    #         None means "not captured", which is different from "" ("captured,
    #         and the agent changed nothing"). Keep that distinction: scoring
    #         will need to tell the two apart.
    # TODO 5: try:
    #             env.start()
    #             exit_status = Agent(messages, env).run()
    #             # SEAM (Stage 5): patch capture goes HERE, inside the try and
    #             # after run(), because the container still exists at this point.
    #             # In the finally it would already be gone.
    #         finally:
    #             env.cleanup()
    #         (Agent is built inside the try so a failure there still cleans up.)
    # TODO 6: return the record dict

    instance = load_instance(instance_id, config["dataset"]["name"], config["dataset"]["split"])
    messages = build_messages(instance, config)

    env = DockerEnvironment(swebench_image_name(instance_id), **config["environment"])
    model_patch = None

    agent = Agent(messages, env, model = config["model"]["name"], **config["agent"])

    try:
        env.start()
        exit_status = agent.run()

    finally:
        env.cleanup()

    return {"instance_id": instance_id, "exit_status": exit_status, "model_patch": model_patch}


# TODO (Step 4c): add an optional flag
#     parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help=...)
#   then config = load_config(args.config) and run_instance(args.instance_id, config).
#   type=Path makes argparse hand you a Path object instead of a str.
def main() -> None:
    """
    Parse the command line and run one instance.

    Returns:
        None. Prints the result record.

    Why:
        Keeps the __main__ guard to one line and makes run_instance() importable
        without side effects (Stage 8's batch runner will import it).
    """
    # TODO 1: parser = argparse.ArgumentParser(description=...)
    # TODO 2: parser.add_argument("instance_id", help=...)  (a required positional)
    # TODO 3: args = parser.parse_args()
    # TODO 4: result = run_instance(args.instance_id); print it

    parser = argparse.ArgumentParser(description = "Run one instance")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Pass the yaml config path for the agent such as: default.yaml")
    parser.add_argument("instance_id", help = "Pass an SWE-bench instance id such as: sympy__sympy-15599")

    args = parser.parse_args()
    config = load_config(args.config)
    result = run_instance(args.instance_id, config)

    print(result)


if __name__ == "__main__":
    main()
