import json
import os
import subprocess
import sys
import threading
import time

GEMINI_DIR = os.path.expanduser("~/.gemini")  # agy stores its config/token under ~/.gemini
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(AGENT_DIR))

# paths inside the container
REASONING_PATH = "/workspace/reasoning.jsonl"
RESULT_PATH = "/workspace/result.json"
STEPS_FILE = "/workspace/agy_steps.jsonl"
BUDGET_FILE = "/workspace/.agy_budget"
PROMPT_FILE = "/workspace/.agy_prompt.txt"
# agy writes a per-conversation transcript under the app-data brain dir.
TRANSCRIPT_GLOB = "/root/.gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl"
# Global customization root where agy discovers hooks.json (per the docs).
HOOKS_PATH = "/root/.gemini/config/hooks.json"

MODEL = "Gemini 3.1 Pro (High)"
LOCATION = "global"  # 3.1 Pro is only served at location "global"
# Wall-clock backstop for agy print mode. The real limit is the step budget
# (enforced by the hook); this just must not cut off a legitimately long solve
# (hard cells run 40+ min for other agents).
PRINT_TIMEOUT = "180m"
# gcp/Vertex project the token is authorized for. Not a secret (a project ID);
# read from the host settings when present, else this default.
PROJECT = "he-based-inference"


class Agent:
    def __init__(self, container_name):
        self.container_name = container_name
        self.reasoning = ""
        self.last_message = ""

    # Hosts agy needs under --no-internet: Vertex (global) + Google OAuth/APIs.
    # NOTE(validate): watch a --no-internet run for connection failures and add
    # any Antigravity control-plane host agy also dials.
    ALLOWED_HOSTS = [
        "aiplatform.googleapis.com",
        "oauth2.googleapis.com",
        "www.googleapis.com",
        "generativelanguage.googleapis.com",
        "antigravity.google",
    ]

    @property
    def ENV(self):
        return {}

    def _exec(self, cmd, **kwargs):
        """Run a command inside the container."""
        env_flags = []
        for k, v in self.ENV.items():
            env_flags += ["-e", f"{k}={v}"]
        return subprocess.run(
            ["docker", "exec"] + env_flags + [self.container_name] + cmd,
            **kwargs,
        )

    def _write_container_file(self, path, content):
        # docker exec needs -i to forward stdin, else `cat >` reads EOF and the
        # file lands empty (which silently broke the agy prompt).
        subprocess.run(
            ["docker", "exec", "-i", self.container_name, "sh", "-c",
             f"mkdir -p $(dirname {path}) && cat > {path}"],
            input=content,
            text=True,
            capture_output=True,
        )

    def auth(self):
        """Copy host ~/.gemini (gcp token + antigravity-cli config) into the
        container, then overlay the canonical run config (model/location,
        headless auto-approve, and the step-budget hook)."""
        if os.path.isdir(GEMINI_DIR):
            subprocess.run(
                ["docker", "cp", f"{GEMINI_DIR}/.", f"{self.container_name}:/root/.gemini"],
                check=True,
                capture_output=True,
            )
        # Canonical settings: pin model + global location, auto-approve tools in
        # headless mode (otherwise -p auto-denies every tool).
        # Prefer the host's gcp.project (deployment-specific); fall back to PROJECT.
        project = PROJECT
        host_settings = os.path.join(GEMINI_DIR, "antigravity-cli", "settings.json")
        try:
            with open(host_settings) as f:
                proj = json.load(f).get("gcp", {}).get("project")
                if proj:
                    project = proj
        except Exception:
            pass
        settings = {
            "enableTelemetry": False,
            "gcp": {"project": project, "location": LOCATION},
            "model": MODEL,
            "trustedWorkspaces": ["/root", "/workspace"],
            "permissions": {"allow": ["command(*)"]},
            "toolExecutionPolicy": "always-proceed",
        }
        self._write_container_file(
            "/root/.gemini/antigravity-cli/settings.json", json.dumps(settings, indent=2)
        )
        # Install the PreToolUse step-budget hook (script baked by install.sh at
        # /opt/agy_hooks; hooks.json in the customization root).
        # NOTE(validate): confirm agy reads hooks.json from ~/.agents/ in headless
        # -p mode; the docs say "customization root (e.g. .agents/hooks.json)".
        # Global customization root (~/.gemini/config/) is where agy discovers hooks.
        hooks_json = os.path.join(AGENT_DIR, "hooks", "hooks.json")
        with open(hooks_json) as f:
            self._write_container_file(HOOKS_PATH, f.read())
        # The Dockerfile only copies install.sh, so ship the hook script at runtime.
        hook_script = os.path.join(AGENT_DIR, "hooks", "step_budget_hook.py")
        with open(hook_script) as f:
            self._write_container_file("/opt/agy_hooks/step_budget_hook.py", f.read())
        self._exec(["sh", "-c", f": > {STEPS_FILE} || true"], capture_output=True)

    def install_mcp(self, tool_manifests):
        """Write agy's mcp_config.json from the harness tool manifests.

        agy consumes MCP natively via ~/.gemini/config/mcp_config.json
        ({"mcpServers": {name: {command, args, env}}}). No `mcp add` step.
        """
        servers = {}
        for m in tool_manifests:
            if "mcp" not in m:
                continue
            mcp = m["mcp"]
            entry = {"command": mcp["command"]}
            if mcp.get("args"):
                entry["args"] = mcp["args"]
            if mcp.get("env"):
                entry["env"] = mcp["env"]
            servers[m["name"]] = entry
        if servers:
            self._write_container_file(
                "/root/.gemini/config/mcp_config.json",
                json.dumps({"mcpServers": servers}, indent=2),
            )

    def run(
        self,
        prompt,
        max_steps=None,
        max_reasoning=None,
        max_tool_timeout=None,
        debug=False,
        count_llm_calls=False,
    ):
        """Run agy headless and return (reasoning, last_message, steps_used, reasoning_used).

        Step budget is enforced in-container by the PreToolUse hook (counts via
        stepIdx, injects the deadline near the limit, denies at the cap). Here we
        launch agy, watch the step file for progress + a no-output watchdog, then
        read the transcript for the trajectory.
        """
        # Publish the budget the hook reads.
        warn_at = 25
        self._write_container_file(BUDGET_FILE, f"{max_steps or 0} {warn_at}")
        self._write_container_file(PROMPT_FILE, prompt)

        proc = subprocess.Popen(
            [
                "docker", "exec", self.container_name,
                "sh", "-c",
                # --dangerously-skip-permissions is agy's equivalent of the other
                # agents' --yolo: auto-approve every tool (not just command()).
                # The PreToolUse hook still fires for step counting + budget deny.
                f'cd /workspace && agy -p "$(cat {PROMPT_FILE})" '
                f'--dangerously-skip-permissions --print-timeout {PRINT_TIMEOUT}',
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        steps = 0
        last_step_time = time.monotonic()
        stop = threading.Event()

        def poll_steps():
            nonlocal steps, last_step_time
            while not stop.is_set():
                r = self._exec(
                    ["sh", "-c", f"wc -l < {STEPS_FILE} 2>/dev/null || echo 0"],
                    capture_output=True, text=True,
                )
                try:
                    n = int((r.stdout or "0").strip() or 0)
                except ValueError:
                    n = steps
                if n > steps:
                    steps = n
                    last_step_time = time.monotonic()
                    if not debug:
                        sys.stdout.write(f"\rStep {steps}   ")
                        sys.stdout.flush()
                stop.wait(3)

        poller = threading.Thread(target=poll_steps, daemon=True)
        poller.start()

        timed_out = False
        while proc.poll() is None:
            time.sleep(2)
            if max_tool_timeout is not None and (time.monotonic() - last_step_time) > max_tool_timeout:
                timed_out = True
                print(f"\n[harness] Tool timeout: no step for {max_tool_timeout}s, killing agent.")
                break

        stdout = ""
        if proc.stdout is not None:
            try:
                stdout = proc.stdout.read() or ""
            except Exception:
                stdout = ""

        if timed_out or proc.poll() is None:
            subprocess.run(["docker", "kill", self.container_name], capture_output=True)
            proc.kill()
        proc.wait()
        stop.set()
        poller.join(timeout=5)
        if not debug:
            sys.stdout.write("\n")

        # last_message = agy's final --print text.
        self.last_message = (stdout or "").strip()

        # Pull the newest per-conversation transcript for the trajectory + counts.
        tpath = self._exec(
            ["sh", "-c", f"ls -t {TRANSCRIPT_GLOB} 2>/dev/null | head -1"],
            capture_output=True, text=True,
        ).stdout.strip()
        transcript = self._read_container_file(tpath) if tpath else ""
        steps_file = self._read_container_file(STEPS_FILE)
        self.reasoning = transcript

        # steps_used = tool-call turns (matches the other agents' step semantics).
        # Prefer the hook's tool-call count; fall back to the transcript.
        steps_used = self._count_steps(steps_file) or self._transcript_tool_calls(transcript) or steps

        # last_message: agy's final --print text, else the last assistant content.
        if not self.last_message:
            self.last_message = self._last_content(transcript)

        # Persist a normalized reasoning trace + result for the viz/grader.
        normalized = self.parse_reasoning(self.reasoning)
        self._write_container_file(
            REASONING_PATH, "\n".join(json.dumps(e) for e in normalized)
        )
        if self.last_message:
            self._write_container_file(RESULT_PATH, self.last_message)

        return self.reasoning, self.last_message, steps_used, len(normalized)

    def _read_container_file(self, path):
        r = self._exec(["sh", "-c", f"cat {path} 2>/dev/null || true"], capture_output=True, text=True)
        return r.stdout or ""

    @staticmethod
    def _count_steps(steps_file):
        return sum(1 for ln in steps_file.splitlines() if ln.strip())

    @staticmethod
    def _transcript_tool_calls(transcript):
        n = 0
        for ln in transcript.splitlines():
            try:
                if json.loads(ln).get("tool_calls"):
                    n += 1
            except json.JSONDecodeError:
                pass
        return n

    @staticmethod
    def _last_content(transcript):
        last = ""
        for ln in transcript.splitlines():
            try:
                obj = json.loads(ln)
                if obj.get("content") and obj.get("source") in ("MODEL", "AGENT"):
                    last = obj["content"]
            except json.JSONDecodeError:
                pass
        return last.strip()

    def cleanup(self):
        """Copy the refreshed gcp token back to the host so it self-heals.

        Deliberately does NOT copy settings.json back: the container's settings
        are this agent's canonical run config, and copying it over the host's
        would strip host-only fields (e.g. gcp.project).
        """
        subprocess.run(
            ["docker", "cp",
             f"{self.container_name}:/root/.gemini/antigravity-cli/antigravity-oauth-token",
             os.path.join(GEMINI_DIR, "antigravity-cli", "antigravity-oauth-token")],
            capture_output=True,
        )

    def get_metrics(self):
        """Token usage from the transcript, if present.

        NOTE(validate): confirm agy's transcript records cumulative token stats
        and the exact key names; return None until validated so cost is blank
        rather than wrong.
        """
        return None

    @staticmethod
    def parse_reasoning(raw_text):
        """Normalize agy's transcript.jsonl into standard viz events.

        agy transcript lines carry: step_index, source, type, status, created_at,
        content, and (for model turns) thinking + tool_calls.
        """
        events = []
        for line in raw_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            ts = obj.get("created_at")
            step = obj.get("step_index")
            typ = obj.get("type", "")
            src = obj.get("source", "")
            thinking = obj.get("thinking")
            tool_calls = obj.get("tool_calls") or []
            content = obj.get("content")

            if thinking:
                events.append({"type": "reasoning", "text": thinking,
                               "timestamp": ts, "metadata": {"step": step}})
            for tc in (tool_calls if isinstance(tool_calls, list) else []):
                name = "tool"
                args = tc
                if isinstance(tc, dict):
                    name = tc.get("name") or tc.get("tool") or next(iter(tc), "tool")
                    args = tc.get("args") or tc.get("arguments") or tc
                events.append({"type": "tool_call",
                               "text": json.dumps(args, ensure_ascii=False),
                               "timestamp": ts,
                               "metadata": {"tool_name": name, "step": step}})
            if content and not thinking and not tool_calls:
                if typ == "USER_INPUT":
                    events.append({"type": "system", "text": "User request",
                                   "timestamp": ts, "metadata": {"subtype": "prompt"}})
                elif src in ("MODEL", "AGENT"):
                    events.append({"type": "reasoning", "text": content,
                                   "timestamp": ts, "metadata": {"step": step}})
                else:
                    events.append({"type": "tool_result", "text": content,
                                   "timestamp": ts, "metadata": {"type": typ}})
        return events
