#!/usr/bin/env python3
"""
Workspace preparation — builds a filtered challenge directory for container mounting.

/workspace/
├── challenge/                      # read-only mount
│   ├── binary
│   ├── evidence/                   # if applicable
│   └── auxilliary_information/     # if applicable
├── available_tools.yaml            # generated from tool manifests
└── submission/                     # writable, agent places artifacts here
"""

import os
import shutil
import tempfile

import yaml

# Files and directories always hidden from the agent.
HIDDEN = ["config.yaml", "eval", "writeup.md", "server"]


def prepare_workspace(challenge_dir, config, tools):
    """
    Build a filtered workspace from the challenge directory.
    Returns the workspace root path.
    """
    workspace = tempfile.mkdtemp(prefix="revbench-workspace-")
    challenge_dest = os.path.join(workspace, "challenge")

    # copy challenge dir, excluding hidden files/dirs. Anchor to the top level
    # only: nested upstream files that happen to share a HIDDEN name (e.g.
    # source/agents/*/config.yaml in a provided source tree) must NOT be stripped.
    def _ignore_top_level(dirpath, names):
        if os.path.abspath(dirpath) == os.path.abspath(challenge_dir):
            return {n for n in names if n in HIDDEN}
        return set()

    shutil.copytree(
        challenge_dir,
        challenge_dest,
        ignore=_ignore_top_level,
    )

    # writable submission dir for agent outputs
    os.makedirs(os.path.join(workspace, "submission"), exist_ok=True)

    # generate available_tools.yaml from tool manifests
    tool_entries = {}
    for tool in tools:
        entry = {}
        if "mcp" in tool:
            entry["mcp"] = True
        if "notes" in tool:
            entry["notes"] = tool["notes"]
        tool_entries[tool["name"]] = entry if entry else None
    with open(os.path.join(workspace, "available_tools.yaml"), "w") as f:
        yaml.dump(tool_entries, f, default_flow_style=False, sort_keys=False)

    return workspace
