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
    config_path = Path(path)

    with config_path.open("r", encoding="utf-8") as file:
        yaml_config = yaml.safe_load(file)

        if not isinstance(yaml_config, dict):
            raise TypeError(f"{config_path} is not a dict, must be a dict for the config to load.")

        return yaml_config


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
    dataset = load_dataset(dataset_name, split = split)

    try:
        instance = next(x for x in dataset if x["instance_id"] == instance_id)

    except StopIteration:
        raise ValueError(f"No instance {instance_id!r} in {dataset_name} ({split})") from None

    return instance


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


def main() -> None:
    """
    Parse the command line and run one instance.

    Returns:
        None. Prints the result record.

    Why:
        Keeps the __main__ guard to one line and makes run_instance() importable
        without side effects (Stage 8's batch runner will import it).
    """
    parser = argparse.ArgumentParser(description = "Run one instance")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Pass the yaml config path for the agent such as: default.yaml")
    parser.add_argument("instance_id", help = "Pass an SWE-bench instance id such as: sympy__sympy-15599")

    args = parser.parse_args()
    config = load_config(args.config)
    result = run_instance(args.instance_id, config)

    print(result)


if __name__ == "__main__":
    main()
