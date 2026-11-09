# =============================================================================
# gepa_adapter.py
# -----------------------------------------------------------------------------
# Responsible for: Connecting the agent to the GEPA optimizer (Stage 11): checking
#                  a proposed pair of prompt templates, running them on instances,
#                  and turning the results into feedback for the reflection model.
#                  So far this holds only the template check (Step 11b.1).
# Role in project: Will be called by the Stage 11 driver through gepa.optimize.
#                  Reuses build_messages from run_single.py, so a template is
#                  checked by the same code that fills it in for a real run.
# Assumptions:     Run from the project root. A candidate is a dict holding
#                  "system_template" and "task_template", the same key names as
#                  the config. config is the loaded config/narration_fix.yaml,
#                  which this module must never change (arm 2 is frozen).
# =============================================================================

import re

from class_agent import ACTION_PATTERN
from run_single import build_messages

# A sentinel value: a string that cannot occur by accident, used to detect
# whether the issue text reached the prompt after formatting.
PROBE_ISSUE = "@@PROBE_ISSUE@@"
PROBE_INSTANCE = {"repo": "probe/probe", "problem_statement": PROBE_ISSUE}


def check_templates(candidate: dict[str, str], config: dict) -> str | None:
    """
    Decide whether a proposed pair of templates can be used for a real run.

    Args:
        candidate: {"system_template": str, "task_template": str} from GEPA.
        config: The loaded config, supplying the values the templates format
            with. Must not be modified.

    Returns:
        None if the templates are usable. Otherwise a one-sentence reason (str)
        that names the problem, written for the reflection model to read,
        because it ends up in the feedback for this candidate.

    Why:
        A guard clause in front of the expensive work: a broken template
        should score 0 before any container starts or any token is spent.
        Checking that str.format succeeds is not enough. Step 11a showed that a
        template without {problem_statement} still formats and would send the
        agent no issue, and that GEPA's text extraction can reduce a reply to
        "<command>". A sentinel value is used instead of searching the template
        for the text "{problem_statement}", because the escaped form
        "{{problem_statement}}" contains that text and still drops the issue.
    """
    # TODO(11b.1): implement the check, in this order. Each failure returns a reason.
    #   a. Build a trial config from config with the candidate's two templates
    #      laid over it, without modifying config itself.
    #   b. Call build_messages(PROBE_INSTANCE, trial_config). A malformed template
    #      raises one of five types, depending on the mistake:
    #      KeyError ({foo}), IndexError ({}), ValueError (a lone brace or a bad
    #      format spec), AttributeError ({timeout.x}), TypeError ({timeout[0]}).
    #      Catch exactly those five, not Exception, and include the type and
    #      message in the reason.
    #   c. messages[1] is the task message. If PROBE_ISSUE is not in its content,
    #      the issue would never reach the agent.
    #   d. messages[0] is the system message. Search it with ACTION_PATTERN
    #      (re.DOTALL, as parse_action does). No match means the model is never
    #      shown the reply format. One match or more is fine.
    #   e. Return None.
    raise NotImplementedError
