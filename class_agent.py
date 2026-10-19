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
    ExecutionTimeout,
    FormatError,
    NonTerminatingException,
    StepLimit,
    Submitted,
    TerminatingException,
)

load_dotenv()


class Agent:
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

        action = self.parse_action(lm_output)
        print("Action", action)
        if action == "exit":
            raise Submitted({"role": "user", "content": "LM requested to quit"})

        action_outcome = self.execute_action(action)
        output = self.format_output(action_outcome)
        print("Output", output)
        self.add_message("user", output)

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
