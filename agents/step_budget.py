"""Shared step-budget deadline signal, used by every agent.

When a run gets within `warn_at` steps of max_steps, write a deadline notice
into the container's /workspace. The image's shell hook (see image_builder.py)
surfaces that file into the agent's next command output, so the agent learns it
must submit. Agent-agnostic: no model-backend or proxy dependency.
"""

import subprocess

WARN_AT = 5
DEADLINE_FILE = "/workspace/.harness_deadline"


def budget_count(steps, llm_calls, count_llm_calls):
    """The count the step budget is measured in: LLM invocations or tool calls."""
    return llm_calls if count_llm_calls else steps


def over_budget(container_name, count, max_steps, warn_at=WARN_AT, subagent_aware=False):
    """Emit the near-limit deadline signal and report whether the budget is spent."""
    signal_if_near_limit(container_name, count, max_steps, warn_at, subagent_aware)
    return max_steps is not None and count >= max_steps


def signal_if_near_limit(
    container_name, steps, max_steps, warn_at=WARN_AT, subagent_aware=False
):
    """Write a countdown deadline notice once the run is within warn_at steps."""
    if not max_steps:
        return
    remaining = max_steps - steps
    if remaining < 0 or remaining > warn_at:
        return
    if subagent_aware:
        msg = (
            f"[HARNESS DEADLINE] {remaining} step(s) remain before this run is "
            "terminated. If you are a sub-agent (spawned via the Agent tool for a "
            "delegated sub-task), stop and return your findings to your parent now; "
            "do not write a submission. Otherwise, stop investigating now and write "
            "your final submission to the path specified in your task immediately."
        )
    else:
        msg = (
            f"[HARNESS DEADLINE] {remaining} step(s) remain before this run is "
            "terminated. Stop investigating now and write your final submission "
            "to the path specified in your task immediately."
        )
    # Pass the message as $0 so its text never has to be shell-escaped.
    subprocess.Popen(
        [
            "docker",
            "exec",
            container_name,
            "sh",
            "-c",
            f"printf '%s' \"$0\" > {DEADLINE_FILE}",
            msg,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
