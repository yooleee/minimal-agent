import os
import re
import subprocess

from dotenv import load_dotenv
from openai import OpenAI

from exceptions import FormatError, NonTerminatingException, TerminatingException

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

env_vars = {
    "PAGER": "CAT", "MANPAGER": "cat",
    "LESS": "-R", "PIP_PROGRESS_BAR": "off",
    "TQDM_DISABLE": "1"
}

def query_lm(messages):

    response = client.responses.create(model="gpt-5.6-luna", input=messages)
    return response.output_text


incorrect_format_message = """Your output was malformated.
Please include exactly 1 action formatted as in the following example:

```bash-action
ls -R
```
"""

def parse_action(lm_output: str) -> str:
    """Takes the LM output, returns the action"""

    matches = re.findall(r"```bash-action\s*\n(.*?)\n```", lm_output, re.DOTALL)

    if not len(matches) == 1:
        raise FormatError(incorrect_format_message)

    return matches[0].strip() if matches else ""


def execute_action(command: str) -> str:
    """Execute action, return the output"""

    if action == "exit":
        raise TerminatingException("LM requested to quit")

    result = subprocess.run(
        command,
        shell=True,
        text=True,
        env=os.environ,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        #check = False
    )

    return result.stdout


# Main Agent Loop

messages = [
    {
        "role": "system",
        "content": "You are a helpful assistant. When you want to run a command, wrap it in ```bash-action\n<command>\n```. To finish, run the exit command.",
    },
    {"role": "user", "content": "List the files in the current directory, then finish."},
]

while True:
    try:
        lm_output = query_lm(messages)
        print("LM Output:", lm_output)
        messages.append(
            {"role": "assistant", "content": lm_output}
        )  # Remember what the LM said

        action = parse_action(lm_output)  # Seperate the action from the output
        print("Action:", action)

        output = execute_action(action)
        print("Output", output)
        messages.append(
            {"role": "user", "content": output}
        )  # Send the command output back

    except NonTerminatingException as e:
        messages.append({"role": "user", "content": str(e)})

    except TerminatingException as e:
        print("Stopping because of ", str(e))
        break
