#!/usr/bin/env python3
"""PreToolUse hook: tool-call step counter + deadline + hard cap for agy.

agy invokes this before every tool call. Since it fires once per tool call, we
count our own invocations (= tool calls, matching the harness step semantics)
rather than agy's all-turns stepIdx. We record each call, inject the harness
deadline near the limit (via `reason`), and hard-deny at the cap. Budget is read
from /workspace/.agy_budget ("<max_steps> <warn_at>").
"""
import json
import sys

STEPS_FILE = "/workspace/agy_steps.jsonl"
BUDGET_FILE = "/workspace/.agy_budget"


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({"decision": "allow"}))
        return

    try:
        with open(BUDGET_FILE) as f:
            max_steps, warn_at = (int(x) for x in f.read().split()[:2])
    except Exception:
        max_steps, warn_at = 0, 25

    # Count prior tool calls (lines already recorded); this call is prior + 1.
    try:
        with open(STEPS_FILE) as f:
            prior = sum(1 for ln in f if ln.strip())
    except Exception:
        prior = 0
    count = prior + 1

    try:
        with open(STEPS_FILE, "a") as f:
            f.write(json.dumps({
                "count": count,
                "stepIdx": payload.get("stepIdx"),
                "tool": (payload.get("toolCall") or {}).get("name"),
            }) + "\n")
    except Exception:
        pass

    out = {"decision": "allow"}
    if max_steps:
        if count >= max_steps:
            out = {
                "decision": "deny",
                "reason": "[HARNESS] Step budget exhausted. Write your final submission now and stop.",
            }
        elif count >= max_steps - warn_at:
            out["reason"] = (
                f"[HARNESS DEADLINE] {max_steps - count} steps left. "
                "Write your final submission to the required location now, then end your turn."
            )
    print(json.dumps(out))


if __name__ == "__main__":
    main()
