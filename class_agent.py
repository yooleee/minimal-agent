import os
import re
import subprocess

from dotenv import load_dotenv
from openai import OpenAI

from config.environments import Environment
from handling.exceptions import (
    ExecutionTimeout,
    FormatError,
    NonTerminatingException,
    StepLimit,
    Submitted,
    TerminatingException,
)

# TODO (Step 2): import the environment types from config.environments:
#   - Environment (the Protocol) for Agent's type hint
#   - DockerEnvironment and swebench_image_name for the temporary __main__ block below
#   `subprocess` stays imported: execute_action still catches subprocess.TimeoutExpired.

load_dotenv()

# TODO (Step 2): the prompt below says "30 seconds" but the environment's timeout
#   will be 60. Edit the number so the model is told the real in-container limit.
#   (Step 4 replaces this hand-sync with a {timeout} placeholder filled from config.)

# TODO (Step 4b): delete system_prompt once config/default.yaml's system_template is
#   filled in and run_single.py renders it (Step 4c). Copy this text into the YAML
#   first: it's the version you've actually been running.


class Agent:
    # TODO (Step 2): add a required `env: Environment` parameter (no default) after
    #   `messages`, and store it as self.env.
    #   Why required: a default LocalEnvironment would silently let the model run
    #   commands on your Mac if a caller forgot to pass one.
    #   Why injected, not constructed here: the caller owns the container's lifecycle
    #   (start/cleanup), and Agent stays testable with LocalEnvironment.
    #
    # TODO (Step 2): move the system-message check from the __main__ block into here.
    #   Run it on the copied list (self.messages), before anything else uses it:
    #   raise ValueError if the list is empty or messages[0]["role"] != "system".
    #   Why here: the invariant belongs to the class that relies on it, so every
    #   caller (including run_single.py in Step 3) gets it without remembering to check.
    #
    # TODO (Step 4b): add keyword parameters max_format_errors=5, output_limit=10000,
    #   head_chars=5000, tail_chars=5000 (same defaults as today, so existing callers and
    #   your LocalEnvironment checks keep working), and store each on self.
    #   Defaults vs config: run_single.py will pass EVERY value from the YAML explicitly,
    #   so for real runs the YAML is the single source. The Python defaults only serve
    #   tests and quick local use.
    #
    # TODO (Step 4b): move the two ValueError checks out of format_output into here:
    #   - head_chars + tail_chars > output_limit
    #   - head_chars < 0 or tail_chars < 0
    #   Why: today they only run when an output happens to exceed the limit, i.e. mid-run
    #   after tokens are spent, and AFTER the slicing they guard. In __init__ a bad config
    #   fails before the first LM call. Use plain string messages (these go to you in a
    #   traceback, not to the model, so they don't need the {"role": ...} dict shape).
    def __init__(
        self, messages: list[dict], env: Environment, model: str = "gpt-5.6-luna",
        max_steps: int = 50, max_format_errors: int = 5, output_limit: int = 10000,
        head_chars: int = 5000, tail_chars: int = 5000
    ):
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = model
        self.env = env
        self.max_steps = max_steps
        self.step_count = 0
        self.max_format_errors = max_format_errors
        self.output_limit = output_limit
        self.head_chars = head_chars
        self.tail_chars = tail_chars
        self.messages = list(messages)

        if not self.messages or self.messages[0]["role"] != "system":
            raise ValueError("Messages must start with a system prompt")

        if head_chars + tail_chars > output_limit:
            raise ValueError("truncation values above limit")

        elif head_chars < 0 or tail_chars < 0:
            raise ValueError("head or tail character limit below 0")

    def add_message(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})

    def query_lm(self):

        if self.step_count >= self.max_steps:
            raise StepLimit({"role": "user", "content": "Step limit reached."})

        self.step_count += 1

        # Pyright ignore used below as message ReportArguemnt Type exception is unwarranted,
        # message dictionary is unverified but not incorrect {system: prompt, "user": prompt}.

        response = self.client.responses.create(model=self.model, input=self.messages) # pyright: ignore[reportArgumentType]

        output_parts = []

        for item in response.output:

            if item.type == "message":
                for parts in item.content:
                    if parts.type == "output_text":
                        output_parts.append(parts.text)


        return "\n".join(output_parts)

    def parse_action(self, lm_output: str) -> str:
        """Takes the LM output, returns the action"""

        matches = re.findall(r"```bash-action\s*\n(.*?)\n```", lm_output, re.DOTALL)

        if len(matches) == 1:
            return matches[0].strip()

        raise FormatError(
            {
                    "role": "user",
                    "content": (
                        f"Found {len(matches)} bash-action blocks; expected exactly one."
                        "Nothing from this reply was executed."
                        "Reply with exactly one bash-action block, or run exit to finish"
                    )
            }
        )

    def execute_action(self, command: str) -> dict:
        """Execute action, return the output"""

        # TODO (Step 2): replace the whole subprocess.run(...) call inside the try
        #   with: result = self.env.execute(command)
        #   - execute() already returns {"output", "returncode"}, so the dict-building
        #     lines at the bottom of this method go away; return the result directly.
        #   - Keep the except subprocess.TimeoutExpired block unchanged: this is the
        #     "Agent translates" layer. Both the in-container timeout (124) and the
        #     host backstop arrive here as TimeoutExpired.
        #   - Keep the bytes/None normalization: the backstop's e.output can be bytes
        #     (seen in the LocalEnvironment test) or None.
        #   - Known wrinkle: if the backstop fired, e.timeout is 75, not 60.


        try:

            result = self.env.execute(command)

        except subprocess.TimeoutExpired as e:

            partial = e.output
            if partial is None:
                partial = "No output before timeout"

            elif isinstance(partial, bytes):
                partial = partial.decode(errors="replace")

            exception_message = {
                "role": "user",
                        "content": (
                            f"Command timed out after {e.timeout} seconds: {command}\n"
                            f"Partial output: \n{partial}\n"
                            "Try a faster or more targetted command")
                    }

            raise ExecutionTimeout(exception_message) from e

        return result

    # TODO (Step 4b): drop the limit/head_chars/tail_chars parameters and read
    #   self.output_limit / self.head_chars / self.tail_chars instead (step() already calls
    #   format_output(action_outcome) with no extra args, so no caller changes).
    #   Delete the two ValueError checks below once they live in __init__.
    def format_output(self, result: dict) -> str:

        action_output  = result["output"] or "(no output)"
        returncode = result["returncode"]

        if len(action_output) <= self.output_limit:
            return f"Exit code: {returncode}\n{action_output}"

        head = action_output[:self.head_chars]
        tail = action_output[-self.tail_chars:]
        removed = len(action_output) - len(head) - len(tail)

        action_output = f"{head}\n--- {removed} characters of content truncated from middle of full output ---\n Save the output to a file and inspect the pieces using grep or sed -n \n{tail}"
        return f"Exit code: {returncode}\n{action_output}"

    def step(self):
        "One complete input-output cycle"

        lm_output = self.query_lm()
        print("LM Output", lm_output)
        self.add_message("assistant", lm_output)

        action = self.parse_action(lm_output)
        print("Action", action)
        if action == "exit":
            raise Submitted({"role": "user", "content": "LM requested to quit"})

        action_outcome = self.execute_action(action)
        output = self.format_output(action_outcome)
        print("Output", output)
        self.add_message("user", output)

    # TODO (Step 4b): replace both hardcoded 5s below (the "/5" in the print and the
    #   `>= 5` check) with self.max_format_errors. Two copies of one number is the DRY
    #   smell: changing one and not the other would print "3/5" while stopping at 3.
    def run(self):
        format_errors = 0

        while True:
            try:
                self.step()
                format_errors = 0

            except FormatError as e:
                reason = type(e).__name__

                format_errors += 1
                print(f"Format Error {format_errors}/{self.max_format_errors}.")
                for message in e.messages:
                    self.messages.append(message)

                if format_errors >= self.max_format_errors:
                    print(f"Stopped: {reason}")
                    return reason

            except NonTerminatingException as e:

                for message in e.messages:
                    self.messages.append(message)

            except TerminatingException as e:
                reason = type(e).__name__

                for message in e.messages:
                    self.messages.append(message)

                print(f"Stopped: {reason}")
                return reason
