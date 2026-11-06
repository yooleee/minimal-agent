# =============================================================================
# class_agent.py
# -----------------------------------------------------------------------------
# Responsible for: The agent loop. Each step queries the LM, parses exactly one
#                  bash-action block from the reply, runs it through the
#                  injected environment, and appends the (truncated) output to
#                  the chat history. run() repeats steps until a terminating
#                  exception (Submitted, StepLimit) or too many format errors.
# Role in project: Built and run by run_single.py, which owns the environment's
#                  start()/cleanup(). The Agent only calls env.execute().
# Contract:        run() returns the exit status as a string (the exception
#                  class name, e.g. "Submitted"). Timeouts from the environment
#                  arrive as subprocess.TimeoutExpired and are turned into an
#                  ExecutionTimeout message for the model here.
# Assumptions:     OPENAI_API_KEY in .env (loaded via python-dotenv). Run from
#                  the project root so `config.` and `handling.` imports resolve.
#                  messages[0] is the system prompt (checked in __init__).
# =============================================================================

import os
import re
import subprocess

from dotenv import load_dotenv
from openai import OpenAI

from config.environments import Environment
from handling.exceptions import (
    EmptyExit,
    ExecutionTimeout,
    FormatError,
    NonTerminatingException,
    PrematureExit,
    StepLimit,
    Submitted,
    TerminatingException,
)

load_dotenv()

# Regex for the bash commands used by the agent. Read by both parse_action
# and narration_measure.

ACTION_PATTERN = r"```bash-action\s*\n(.*?)\n```"

# Stage 10b (narration_fix). Added after the command's output, so the message
# still starts with "Exit code:" or "Command timed out".
DROPPED_BLOCKS_NOTE = (
    "\n\nNote: your reply had {total} bash-action blocks. Only the first one ran. "
    "Blocks dropped without running: {dropped}. "
    "Send one block per reply, and base the next one on the output above."
)

# Starts with the prefix narration_measure.py uses to count this rejection.
EMPTY_EXIT_MESSAGE = (
    "Exit refused: the repository has no changes, so there is nothing to submit. "
    "Any edit in an earlier reply that was rejected or dropped did not run. "
    "Make the change, verify it, then exit."
)

class Agent:
    def __init__(
        self, messages: list[dict], env: Environment, model: str = "gpt-5.6-luna",
        max_steps: int = 50, max_format_errors: int = 5, output_limit: int = 10000,
        head_chars: int = 5000, tail_chars: int = 5000, narration_fix: bool = False
    ):
        self.client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self.model = model
        self.env = env
        self.max_steps = max_steps
        self.step_count = 0
        self.commands_run = 0
        self.max_format_errors = max_format_errors
        self.output_limit = output_limit
        self.head_chars = head_chars
        self.tail_chars = tail_chars
        self.messages = list(messages)
        self.lm_calls: list[dict] = []
        self.narration_fix = narration_fix
        self.read_patch = None

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
        usage = response.usage

        message_items = []

        for item in response.output:

            if item.type == "message":
                message_parts = []
                for parts in item.content:
                    if parts.type == "output_text":
                        message_parts.append(parts.text)

                message_items.append({"phase": item.phase, "text": "\n".join(message_parts)})

        self.lm_calls.append({
            "step": self.step_count,
            "output_types": [item.type for item in response.output],
            "message_items": message_items,
            "input_tokens": None if usage is None else usage.input_tokens,
            "output_tokens": None if usage is None else usage.output_tokens,
            "reasoning_tokens": None if usage is None else usage.output_tokens_details.reasoning_tokens,
            "cached_tokens": None if usage is None else usage.input_tokens_details.cached_tokens,
        })

        return "\n".join(item["text"] for item in message_items)

    def parse_action(self, lm_output: str) -> tuple[str, int]:
        """
        Take the LM output and return the action to run.

        Args:
            lm_output: The model's reply.

        Returns:
            (the action, how many blocks of the reply were dropped). dropped
            is 0 unless narration_fix is on and the reply had several blocks.

        Raises:
            FormatError: for a reply with no block, or with several blocks
                when narration_fix is off.

        Why:
            A reply that narrates a whole session used to be rejected whole,
            so a first command that was fine never ran, about 1 step in 5 was
            lost (Step 10a), and an edit in such a reply could vanish
            (django-11333). Running the first block keeps the one-command loop:
            later blocks were written before seeing any output, so they are
            guesses. An exit at the end of a narrated reply is dropped too, as
            it was never the first block in the pool (0 of 182).
        """
        matches = re.findall(ACTION_PATTERN, lm_output, re.DOTALL)

        if len(matches) == 1:
            return matches[0].strip(), 0

        if self.narration_fix and len(matches) >= 2:
            return matches[0].strip(), len(matches) - 1

        raise FormatError(
            {
                    "role": "user",
                    "content": (
                        f"Found {len(matches)} bash-action blocks; expected exactly one."
                        "\nNothing from this reply was executed."
                        "\nReply with exactly one bash-action block, or run exit to finish"
                    )
            }
        )

    def execute_action(self, command: str) -> dict:
        """Execute action, return the output"""

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

        action, dropped = self.parse_action(lm_output)

        if action == "exit" and self.commands_run == 0:
            raise PrematureExit({"role": "user", "content": "Nothing has been executed yet, please start with a command"})

        print("Action", action)
        if action == "exit":
            if self.narration_fix and self.read_patch() == "":
                raise EmptyExit({"role": "user", "content": EMPTY_EXIT_MESSAGE})

            raise Submitted({"role": "user", "content": "LM requested to quit"})

        self.commands_run += 1
        dropped_string = ""
        if dropped > 0:
            dropped_string = DROPPED_BLOCKS_NOTE.format(total=dropped + 1, dropped=dropped)

        try:
            action_outcome = self.execute_action(action)

        except ExecutionTimeout as e:
            e.messages[0]["content"] += dropped_string
            raise

        output = self.format_output(action_outcome)
        output += dropped_string
        print("Output", output)
        self.add_message("user", output)

    def run(self, read_patch=None):
        """
        Run steps until a terminating exception or too many format errors.

        Args:
            read_patch: A function with no arguments returning the current
                patch ("" if nothing changed, None if it could not be read).
                Required when narration_fix is on.

        Returns:
            The exit status, the exception class name such as "Submitted".

        Raises:
            ValueError: if narration_fix is on and read_patch is None.

        Why:
            Dependency injection: run_single owns capture_patch and the start
            commit, and it imports this module, so the Agent cannot import
            them back. Passing a function keeps the check identical to what is
            submitted, and a test can pass a stub instead of a container.
        """
        if self.narration_fix:

            if read_patch is not None:
                self.read_patch = read_patch

            else:
                raise ValueError(f"Narration_fix is on but no read_patch function was given: {read_patch}")

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
