#!/usr/bin/env python3
import argparse
import csv
import glob
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

import yaml
from evaluator import evaluate
from image_builder import get_image
from network import NETWORK_NAME, setup_isolation, teardown_isolation
from prompt_builder import build_revdeflate_prompt
from server import (
    _get_license_server_image,
    build_server_image,
    create_network,
    get_server_ip,
    remove_network,
    start_server,
    stop_server,
    wait_for_server,
)
from workspace import prepare_workspace

# --- Discovery: locate available binaries, tools, and agents ---

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def load_dotenv():
    """Load KEY=VALUE lines from the repo-root .env into os.environ without
    overriding vars already set in the environment."""
    path = os.path.join(ROOT, ".env")
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def extract_model(agent, reasoning):
    """Best-effort actual model id. Perplexity agents declare a MODEL; codex
    captures it from its session log (self.model); native claude/gemini emit it
    in their stream-json."""
    model = getattr(agent, "MODEL", None) or getattr(agent, "model", None)
    if model:
        return model
    for line in reasoning.splitlines():
        if '"model"' not in line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        m = obj.get("model") or (obj.get("message") or {}).get("model")
        if isinstance(m, str) and m:
            return m
    return None


BINARIES_DIRS = [
    os.path.join(ROOT, "binaries", "revdeflate"),
]
# These types have short leaf names (c-opt-sym-static, ...) that would collide
# across types, so their invocation key is namespaced by type, e.g.
# "revdeflate/c-opt-sym-static".
NAMESPACED_TYPE_DIRS = (
    "revdeflate",
)
TOOLS_DIR = os.path.join(ROOT, "tools")
AGENTS_DIR = os.path.join(ROOT, "agents")

AVAILABLE_AGENTS = [
    name
    for name in sorted(os.listdir(AGENTS_DIR))
    if os.path.isfile(os.path.join(AGENTS_DIR, name, "agent.py"))
]


def load_json(path):
    with open(path) as f:
        return json.load(f)


def load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def discover_binaries():
    # binaries/revcompress/c-opt-sym-static/config.yaml → "revcompress/c-opt-sym-static"
    # binaries/revfirmware/c-opt/config.yaml            → "revfirmware/c-opt"

    binaries = {}
    for binaries_dir in BINARIES_DIRS:
        if not os.path.isdir(binaries_dir):
            continue

        prefix = os.path.basename(binaries_dir)
        all_challenges = glob.glob(
            os.path.join(binaries_dir, "**", "config.yaml"), recursive=True
        )
        for config_path in all_challenges:
            challenge_dir = os.path.dirname(config_path)
            key = os.path.relpath(challenge_dir, binaries_dir)
            if prefix in NAMESPACED_TYPE_DIRS:
                key = f"{prefix}/{key}"
            config = load_yaml(config_path)
            # skip stray config.yaml files vendored inside challenge source trees
            # (e.g. custom_agent defs); real challenge configs always set a type
            if not isinstance(config, dict) or "type" not in config:
                continue
            config["_dir"] = challenge_dir
            binaries[key] = config
    return binaries


def resolve_problem_type(binary_config):
    return binary_config["type"]


def needs_server(binary_config):
    # A `server:` block means a live device sidecar the agent probes (revfirmware).
    # The packer license sidecar (a `license:` block) is separate and always
    # started -- see the run body.
    return binary_config.get("server") is not None


def resolve_tools(tier, binary_config):
    bash = ["bash"]
    if tier == "tier1":
        tool_names = bash
    elif tier == "tier2":
        tool_names = bash + list(binary_config.get("tools", {}).keys())
    else:  # tier 3
        all_tools = [
            n
            for n in sorted(os.listdir(TOOLS_DIR))
            if os.path.isfile(os.path.join(TOOLS_DIR, n, "manifest.json"))
        ]
        tool_names = bash + [t for t in all_tools if t != "bash"]
    return [load_json(os.path.join(TOOLS_DIR, t, "manifest.json")) for t in tool_names]


# --- Runtime: agent loading, container lifecycle, and service management ---


def load_agent_module(agent_name):
    # load agent module at runtime
    sys.path.insert(0, os.path.join(AGENTS_DIR, agent_name))
    return importlib.import_module("agent")


def start_container(
    container_name, image_tag, workspace_dir, network=None, extra_hosts=None
):
    # start the container and mount the prepared workspace tree
    run_cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "-v",
        f"{workspace_dir}:/workspace",
    ]
    if network:
        run_cmd += ["--network", network]
    if extra_hosts:
        for host, ip in extra_hosts.items():
            run_cmd += ["--add-host", f"{host}:{ip}"]
    run_cmd.append(image_tag)
    subprocess.run(run_cmd, check=True, capture_output=True)
    print(f"Container {container_name} started.")


def remove_env_binaries(container_name, names):
    """Remove named binaries from the running agent container before the agent
    starts. Per-challenge env ablation driven by config `env_remove:` -- a
    challenge can drop specific CLI tools so a decode can't be shortcut with a
    stock tool. (Does not remove python/zlib, so formats are still decodable; this
    only removes the named CLI tools.)"""
    if not names:
        return
    names_str = " ".join(str(n) for n in names)
    script = (
        f"for b in {names_str}; do "
        f'for p in "$(command -v "$b" 2>/dev/null)" "/usr/bin/$b" "/bin/$b" "/usr/local/bin/$b"; '
        f'do [ -n "$p" ] && rm -f "$p"; done; done; true'
    )
    subprocess.run(
        ["docker", "exec", container_name, "bash", "-lc", script],
        capture_output=True,
    )
    print(f"env_remove: dropped [{names_str}] from the agent container")


def start_services(container_name, tools):
    # start the background services
    for tool in tools:
        if "service" not in tool:
            continue
        service = tool["service"]
        # Substitute {{BINARY}} placeholder with actual binary path in container
        service_args = [
            arg.replace("{{BINARY}}", "/workspace/challenge/binary")
            for arg in service.get("args", [])
        ]
        cmd = [service["command"]] + service_args

        print(f"Starting {tool['name']} service...")
        result = subprocess.run(
            ["docker", "exec", container_name] + cmd,
            capture_output=True,
            text=True,
            timeout=service.get("timeout", 180),
        )

        ready_marker = service.get("ready_marker")
        if ready_marker and ready_marker in result.stdout:
            print(f"{tool['name']} service ready")
        else:
            print(f"WARNING: {tool['name']} service may have failed to start")
            if result.stdout:
                print(f"stdout: {result.stdout[-500:]}")
            if result.stderr:
                print(f"stderr: {result.stderr[-500:]}")


# --- Results: reporting and output ---


def save_results(report, reasoning, binary_name, tier, run_id):
    # save results
    results_dir = os.path.join(ROOT, "results")
    os.makedirs(results_dir, exist_ok=True)
    safe_name = binary_name.replace("/", "-")

    result_file = os.path.join(results_dir, f"{safe_name}_{tier}_{run_id}.json")
    with open(result_file, "w") as f:
        json.dump(report, f, indent=2)

    reasoning_file = os.path.join(
        results_dir, f"{safe_name}_{tier}_{run_id}_reasoning.jsonl"
    )
    with open(reasoning_file, "w") as f:
        f.write(reasoning)

    return result_file, reasoning_file


def save_submission(submission_dir, binary_name, tier, run_id):
    # archive the agent's submission tree before the workspace is torn down
    if not os.path.isdir(submission_dir) or not os.listdir(submission_dir):
        return None
    results_dir = os.path.join(ROOT, "results")
    os.makedirs(results_dir, exist_ok=True)
    safe_name = binary_name.replace("/", "-")
    archive_base = os.path.join(results_dir, f"{safe_name}_{tier}_{run_id}_submission")
    try:
        return shutil.make_archive(archive_base, "gztar", submission_dir)
    except PermissionError:
        # The agent runs as root and can leave root-owned, restrictive-mode files
        # (e.g. revcompress reproduces graded permission bits) that an unprivileged
        # harness user cannot read. Archive via a root-privileged tar (the docker
        # daemon runs as root), preserving the exact modes.
        out = archive_base + ".tar.gz"
        src = os.path.abspath(submission_dir)
        with open(out, "wb") as fh:
            subprocess.run(
                ["docker", "run", "--rm", "--network", "none",
                 "-v", f"{src}:/s:ro", "busybox",
                 "tar", "-C", "/s", "--numeric-owner", "-czf", "-", "."],
                stdout=fh,
                check=True,
            )
        return out


def _score_str(result):
    ai = result.get("additional_info") or {}
    if "score" in ai:
        return f"{ai.get('score', 0)}/{ai.get('max', 6)}"
    return ""


def print_round(report):
    result = report["result"]
    tokens = report.get("tokens") or {}
    print(f"\nCorrect: {result['correct']}")
    score = _score_str(result)
    if score:
        print(f"Score:   {score}")
    print(f"Time:  {report['wall_time']}s")
    print(f"Steps: {report['steps_used']}")
    print(f"Reasoning: {report['reasoning_used']}")
    if tokens:
        print(f"Input tokens:        {tokens.get('input_tokens', 0)}")
        print(f"Cached input tokens: {tokens.get('cached_input_tokens', 0)}")
        print(f"Output tokens:       {tokens.get('output_tokens', 0)}")
        print(f"Total tokens:        {tokens.get('total_tokens', 0)}")


def write_summary(round_results, args, base_id):
    safe_name = args.binary.replace("/", "-")
    results_dir = os.path.join(ROOT, "results")
    os.makedirs(results_dir, exist_ok=True)
    total = len(round_results)
    if total == 0:
        print("No rounds completed.")
        return

    solve_count = sum(1 for r in round_results if r["result"]["correct"])
    avg_steps = sum(r["steps_used"] for r in round_results) / total
    avg_reasoning = sum(r["reasoning_used"] for r in round_results) / total
    avg_wall = sum(r["wall_time"] for r in round_results) / total

    # CSV
    csv_path = os.path.join(
        results_dir, f"{safe_name}_{args.tier}_{base_id}_summary.csv"
    )
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "round",
                "correct",
                "score",
                "steps_used",
                "reasoning_used",
                "wall_time",
                "input_tokens",
                "output_tokens",
                "reasoning_trace",
            ]
        )
        for i, r in enumerate(round_results, 1):
            tokens = r.get("tokens") or {}
            writer.writerow(
                [
                    i,
                    r["result"]["correct"],
                    _score_str(r["result"]),
                    r["steps_used"],
                    r["reasoning_used"],
                    r["wall_time"],
                    tokens.get("input_tokens", 0),
                    tokens.get("output_tokens", 0),
                    os.path.basename(r.get("reasoning_file", "")),
                ]
            )

    # Per-round table
    col_w = [5, 7, 7, 10, 10, 13, 14]
    headers = [
        "Round",
        "Correct",
        "Steps",
        "Reasoning",
        "Wall(s)",
        "Input Tokens",
        "Output Tokens",
    ]
    sep = "-+-".join("-" * w for w in col_w)

    def fmt_row(cells):
        return " | ".join(str(c).ljust(w) for c, w in zip(cells, col_w))

    print(f"\n{'=' * 40}")
    if total < args.rounds:
        print(f"{total}/{args.rounds} rounds complete (early stop)")
    else:
        print(f"{total} rounds complete")
    print(f"{'=' * 40}")
    print(fmt_row(headers))
    print(sep)
    for i, r in enumerate(round_results, 1):
        tokens = r.get("tokens") or {}
        correct_str = "yes" if r["result"]["correct"] else "no"
        print(
            fmt_row(
                [
                    i,
                    correct_str,
                    r["steps_used"],
                    r["reasoning_used"],
                    f"{r['wall_time']:.1f}",
                    tokens.get("input_tokens", 0),
                    tokens.get("output_tokens", 0),
                ]
            )
        )
    print(sep)
    print(
        fmt_row(
            [
                "avg",
                f"{solve_count / total * 100:.0f}%",
                f"{avg_steps:.1f}",
                f"{avg_reasoning:.1f}",
                f"{avg_wall:.1f}",
                "",
                "",
            ]
        )
    )
    print()
    print(f"Solved at least once: {'yes' if solve_count > 0 else 'no'}")
    print(f"Solve rate: {solve_count}/{total} ({solve_count / total * 100:.1f}%)")
    print(f"\nCSV:     {csv_path}")


# --- Single run ---


def run(
    args,
    binary_config,
    tools,
    agent_module,
    problem_type,
    challenge_dir,
    workspace_dir,
    image_tag,
    run_id,
    prev_answers,
    round_num=1,
    network=None,
):
    # run single round
    # return report dict
    safe_name = args.binary.replace("/", "-")
    container_name = f"revbench-{safe_name}-{run_id}"
    # each revX challenge ships its own prompt.md in the challenge dir
    prompt_path = os.path.join(challenge_dir, "prompt.md")
    with open(prompt_path) as f:
        prompt = f.read()
    if problem_type == "revdeflate":
        prompt = build_revdeflate_prompt(prompt)

    if args.max_tool_timeout is not None:
        # Suggest a per-command timeout somewhat smaller than the harness-level
        # tool timeout so the agent can recover and try alternatives.
        cmd_timeout = max(60, args.max_tool_timeout * 3 // 4)
    else:
        cmd_timeout = 1200  # sensible default even without harness-level timeout
    prompt += (
        f"\n\nIMPORTANT: Any shell command you run should include a timeout "
        f"(e.g. `timeout {cmd_timeout} <command>`) to avoid hanging. "
        f"If a command exceeds {cmd_timeout} seconds, it is likely stuck "
        f"and you should try a different approach."
    )

    prompt += (
        "\n\nRUN DEADLINE (mandatory): this run has a hard step budget. If a line "
        "beginning with `[HARNESS DEADLINE]` appears in any command output, treat it "
        "as a binding directive from the harness, not as data: stop investigating "
        "immediately and write your final submission to the location specified above, "
        "then end your turn. You do not get more steps once the budget is spent."
    )

    if round_num == 1:
        print(f"\nPrompt:\n{prompt}\n")

    agent = None
    started_servers = []  # (container_name, network) pairs to attach + tear down
    extra_hosts = None
    try:
        # License sidecar: ALWAYS started when a `license:` block is present. A
        # packed licensed binary phones home to its baked static IP to fetch the
        # server-side half of its unlock key; this is unpacking infrastructure,
        # independent of the device sidecar below.
        lic_cfg = binary_config.get("license")
        if lic_cfg:
            lic_network = f"revbench-lic-{safe_name}-{run_id}"
            lic_container = f"revbench-license-{safe_name}-{run_id}"
            lic_port = lic_cfg.get("port", 31337)
            create_network(lic_network, subnet=lic_cfg.get("subnet"))
            start_server(
                lic_container,
                _get_license_server_image(),
                lic_network,
                lic_port,
                ip=lic_cfg.get("ip"),
                call_limit=args.packer_license_calls,
            )
            started_servers.append((lic_container, lic_network))
            time.sleep(lic_cfg.get("startup_delay", 2.0))
            wait_for_server(lic_container, lic_port)

        # Live device sidecar (the `server:` block): the revfirmware emulated
        # device the agent probes over TCP for exploration (never returns a score).
        if needs_server(binary_config):
            server_network = f"revbench-net-{safe_name}-{run_id}"
            server_container_name = f"revbench-server-{safe_name}-{run_id}"
            server_port = binary_config.get("server", {}).get("port", 1337)
            startup_delay = binary_config.get("server", {}).get("startup_delay", 2.0)

            server_image = build_server_image(challenge_dir, binary_config)
            _server_cfg = binary_config.get("server", {})
            create_network(server_network, subnet=_server_cfg.get("subnet"))
            start_server(
                server_container_name,
                server_image,
                server_network,
                server_port,
                firmware_variant=binary_config.get("variant"),
            )
            started_servers.append((server_container_name, server_network))
            time.sleep(startup_delay)
            wait_for_server(server_container_name, server_port)

        start_container(
            container_name,
            image_tag,
            workspace_dir,
            network=network,
            extra_hosts=extra_hosts,
        )

        # Attach agent to each internal server network (license sidecar and/or
        # device server) so it can reach them. The agent's primary network handles
        # API access (isolation network if --no-internet, default bridge otherwise).
        for _cont, _net in started_servers:
            subprocess.run(
                ["docker", "network", "connect", _net, container_name],
                check=True,
                capture_output=True,
            )
        agent = agent_module.Agent(container_name)
        agent.auth()
        agent.install_mcp(tools)
        start_services(container_name, tools)
        remove_env_binaries(container_name, binary_config.get("env_remove") or [])

        t0 = time.time()
        reasoning, last_message, steps_used, reasoning_used = agent.run(
            prompt,
            max_steps=args.max_steps,
            max_reasoning=args.max_reasoning,
            max_tool_timeout=args.max_tool_timeout,
            debug=args.debug,
            count_llm_calls=args.count_llm_calls,
        )
        wall = time.time() - t0
        tokens = agent.get_metrics() if hasattr(agent, "get_metrics") else None

        # save reasoning first so the judge can reference it
        partial_report = {
            "binary": args.binary,
            "tier": args.tier,
            "agent": args.agent,
            "model": extract_model(agent, reasoning),
            "tools": [t["name"] for t in tools],
            "problem_type": problem_type,
            "max_steps": args.max_steps,
            "count_llm_calls": args.count_llm_calls,
            "max_reasoning": args.max_reasoning,
            "max_tool_timeout": args.max_tool_timeout,
            "no_internet": args.no_internet,
            "rounds": args.rounds,
            "early_stop": args.early_stop,
            "round_num": round_num,
            "steps_used": steps_used,
            "reasoning_used": reasoning_used,
            "wall_time": round(wall, 1),
            "last_message": last_message,
            "tokens": tokens,
            "result": None,
        }
        result_file, reasoning_file = save_results(
            partial_report, reasoning, args.binary, args.tier, run_id
        )

        # evaluate the result in the submission inbox
        submission_dir = os.path.join(workspace_dir, "submission")
        result = evaluate(
            binary_config, challenge_dir, submission_dir, reasoning_file, agent
        )

        # archive the submission before teardown clears the workspace
        submission_file = save_submission(
            submission_dir, args.binary, args.tier, run_id
        )

        # Print eval output if present
        for key in ("stdout", "stderr", "judge_output"):
            val = result.get("additional_info", {}).get(key, "")
            if val:
                print(f"\n[eval {key}]\n{val}")

        report = partial_report
        report["result"] = result
        report["result_file"] = result_file
        report["reasoning_file"] = reasoning_file
        report["submission_file"] = submission_file

        # re-save with the result filled in
        with open(result_file, "w") as f:
            json.dump(report, f, indent=2)

        print_round(report)
        print(f"Saved to {result_file}")
        if submission_file:
            print(f"Saved submission to {submission_file}")
        return report
    finally:
        if agent is not None and hasattr(agent, "cleanup"):
            agent.cleanup()
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        for _cont, _net in started_servers:
            stop_server(_cont)
            remove_network(_net)


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="RevBench controller")
    parser.add_argument("binary", nargs="?", help="Binary name to run")
    parser.add_argument("--tier", choices=["tier1", "tier2", "tier3"], help="Tool tier")
    parser.add_argument("--agent", choices=AVAILABLE_AGENTS, help="Agent to use")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=200,
        help="Max counted agent steps (command/MCP completions) before stopping",
    )
    parser.add_argument(
        "--count-llm-calls",
        action="store_true",
        help="Count LLM API invocations toward --max-steps instead of tool calls",
    )
    parser.add_argument(
        "--max-reasoning",
        type=int,
        default=None,
        help="Max agent reasoning messages before stopping",
    )
    parser.add_argument(
        "--max-tool-timeout",
        type=int,
        default=None,
        help="Max seconds to wait for a single tool invocation before killing the agent (e.g. 600 for 10 min)",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1,
        help="Number of independent runs (default: 1)",
    )
    parser.add_argument(
        "--early-stop",
        action="store_true",
        help="Stop early if a round succeeds (default: run all rounds)",
    )
    parser.add_argument(
        "--list-binaries", action="store_true", help="List available binaries"
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Do not delete the workspace directory after the run; print its path",
    )
    parser.add_argument(
        "--no-internet",
        action="store_true",
        help="Isolate container networking: block all internet except the agent's required API endpoints",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Stream raw agent JSON output to stdout instead of a refreshing status line",
    )
    parser.add_argument(
        "--packer-license-calls",
        type=int,
        default=None,
        help="Cap how many license grants the revpacker licensed sidecar will serve "
        "before refusing further requests (limits run-and-dump page-harvest across "
        "many launches). Omitted = unlimited.",
    )
    args = parser.parse_args()

    if args.list_binaries:
        for name, binary_config in discover_binaries().items():
            tools = ", ".join(binary_config.get("tools", {}).keys()) or "none"
            pt = binary_config["type"]
            print(f"  {name:<40} {pt:<20} tools: {tools}")
        return

    if not args.binary or not args.tier or not args.agent:
        parser.error("binary, --tier, and --agent are required")

    binaries = discover_binaries()
    if args.binary not in binaries:
        parser.error(f"unknown binary '{args.binary}'. Use --list to see options.")

    binary_config = binaries[args.binary]
    tools = resolve_tools(args.tier, binary_config)
    agent_module = load_agent_module(args.agent)
    problem_type = binary_config["type"]
    challenge_dir = os.path.abspath(binary_config["_dir"])

    print(f"Binary:  {args.binary}")
    print(f"Tier:    {args.tier}")
    print(f"Agent:   {args.agent}")
    print(f"Tools:   {', '.join(t['name'] for t in tools)}")
    print(f"Type:    {problem_type}")
    print(f"Step limit:   {args.max_steps}")
    print(f"Reasoning limit: {args.max_reasoning}")
    print(f"Rounds:  {args.rounds}")

    image_tag = get_image(args.agent, tools)

    # Set up network isolation if requested
    network = None
    if args.no_internet:
        allowed_hosts = getattr(agent_module.Agent, "ALLOWED_HOSTS", None)
        if not allowed_hosts:
            parser.error(
                f"agent '{args.agent}' does not define ALLOWED_HOSTS; "
                f"cannot use --no-internet"
            )
        setup_isolation(allowed_hosts)
        network = NETWORK_NAME
        print(f"Network isolation enabled (allowed: {', '.join(allowed_hosts)})")

    workspace_dir = None
    try:
        # prep workspace this will be reused every run and cleaned up at the end
        workspace_dir = prepare_workspace(challenge_dir, binary_config, tools)
    except OSError as exc:
        parser.error(f"failed to prepare workspace: {exc}")

    base_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    round_results = []
    prev_answers = []

    try:
        for round_num in range(1, args.rounds + 1):
            if args.rounds > 1:
                print(f"\n{'=' * 40}")
                print(f"Round {round_num}/{args.rounds}")
                print(f"{'=' * 40}")

            run_id = f"{args.agent}_{base_id}_r{round_num}"
            try:
                report = run(
                    args,
                    binary_config,
                    tools,
                    agent_module,
                    problem_type,
                    challenge_dir,
                    workspace_dir,
                    image_tag,
                    run_id,
                    round_num=round_num,
                    prev_answers=prev_answers,
                    network=network,
                )
                # removed feedback loop for now
                # if not report["result"]["correct"]:
                #     # for open-ended, feed back the answer_summary (what the agent claimed)
                #     # so the next round can avoid repeating the same wrong hypothesis
                #     summary = report["result"].get("answer_summary", "")
                #     if summary and summary != "NOT FOUND":
                #         prev_answers.append(summary)
                #     else:
                #         prev_answers.append(report["result"]["agent_answer"])
            except Exception as e:
                # if failed run empty report
                print(f"Round {round_num} failed: {e}")
                report = {
                    "binary": args.binary,
                    "tier": args.tier,
                    "agent": args.agent,
                    "tools": [t["name"] for t in tools],
                    "problem_type": problem_type,
                    "max_steps": args.max_steps,
                    "max_reasoning": args.max_reasoning,
                    "max_tool_timeout": args.max_tool_timeout,
                    "no_internet": args.no_internet,
                    "rounds": args.rounds,
                    "early_stop": args.early_stop,
                    "round_num": round_num,
                    "result": {
                        "correct": False,
                        "additional_info": {},
                    },
                    "steps_used": 0,
                    "reasoning_used": 0,
                    "wall_time": 0,
                    "tokens": None,
                    "reasoning_file": "",
                }
            finally:
                # always clear submission between rounds
                sub_dir = os.path.join(workspace_dir, "submission")
                if os.path.isdir(sub_dir):
                    # ignore_errors: an agent can leave root-owned, restrictive-mode
                    # dirs here (e.g. revcompress reproduces graded permission bits);
                    # an unprivileged harness user cannot rmtree those, and the fresh
                    # workspace below is what the next round uses regardless.
                    shutil.rmtree(sub_dir, ignore_errors=True)
                    os.makedirs(sub_dir, exist_ok=True)

                if not args.keep_workspace:
                    # full workspace reset between rounds to prevent leak
                    shutil.rmtree(workspace_dir, ignore_errors=True)
                    workspace_dir = prepare_workspace(
                        challenge_dir, binary_config, tools
                    )

            round_results.append(report)

            if report["result"]["correct"] and args.early_stop:
                if args.rounds > 1:
                    print(
                        f"\n[Early Stop] Success in round {round_num}, stopping further rounds."
                    )
                break

        if args.rounds > 1 or any(r["result"]["correct"] for r in round_results):
            write_summary(round_results, args, base_id)
    finally:
        # clean up workspace that was temp created on host
        if workspace_dir:
            if args.keep_workspace:
                print(f"\nWorkspace kept at: {workspace_dir}")
            else:
                shutil.rmtree(workspace_dir, ignore_errors=True)
        # tear down network isolation
        if args.no_internet:
            teardown_isolation()


if __name__ == "__main__":
    main()
