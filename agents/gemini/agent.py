import json
import os
import subprocess
import sys
import threading
import tempfile
import shutil
import time

GEMINI_DIR = os.path.expanduser("~/.gemini")
AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(AGENT_DIR))
from step_budget import over_budget  # noqa: E402

# paths inside the container
REASONING_PATH = "/workspace/reasoning.jsonl"
RESULT_PATH = "/workspace/result.json"


class Agent:
    def __init__(self, container_name):
        self.container_name = container_name
        self.reasoning = ""
        self.last_message = ""
        self.sessions_dir = tempfile.mkdtemp(prefix="revbench-gemini-sessions-")

    # Hosts required for Gemini CLI API calls, auth, and telemetry.
    ALLOWED_HOSTS = [
        "generativelanguage.googleapis.com",
        "oauth2.googleapis.com",
        "aiplatform.googleapis.com",
        "us-central1-aiplatform.googleapis.com",
        "www.googleapis.com",
    ]

    @property
    def ENV(self):
        env = {}
        api_key = os.environ.get("GEMINI_API_KEY")
        if api_key:
            env["GEMINI_API_KEY"] = api_key
        return env

    def _exec(self, cmd, **kwargs):
        """Run a command inside the container."""
        env_flags = []
        for k, v in self.ENV.items():
            env_flags += ["-e", f"{k}={v}"]
        return subprocess.run(
            ["docker", "exec"] + env_flags + [self.container_name] + cmd,
            **kwargs,
        )

    def auth(self):
        """Copy host ~/.gemini into the container for auth,
        then overlay a fresh sessions dir to isolate this run."""
        if os.path.isdir(GEMINI_DIR):
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{GEMINI_DIR}/.",
                    f"{self.container_name}:/root/.gemini",
                ],
                check=True,
                capture_output=True,
            )
        self._exec(["rm", "-rf", "/root/.gemini/sessions"], capture_output=True)
        self._exec(["mkdir", "-p", "/root/.gemini/sessions"], capture_output=True)

    def install_mcp(self, tool_manifests):
        """Register MCP servers from tool manifests with gemini."""
        for m in tool_manifests:
            if "mcp" not in m:
                continue
            mcp = m["mcp"]
            # Syntax: gemini mcp add <name> <commandOrUrl> [args...]
            cmd = ["gemini", "mcp", "add", m["name"], mcp["command"]]
            cmd += mcp.get("args", [])
            self._exec(cmd, check=True, capture_output=True)

    def _parse_event(self, line):
        """Parse a stream-json line and return (event_type, role) or (None, None)."""
        try:
            obj = json.loads(line)
            return obj.get("type"), obj.get("role")
        except (json.JSONDecodeError, AttributeError):
            return None, None

    def run(
        self,
        prompt,
        max_steps=None,
        max_reasoning=None,
        max_tool_timeout=None,
        debug=False,
        count_llm_calls=False,
    ):
        """Run gemini and return (reasoning, last_message, steps_used, reasoning_used)."""
        env_flags = []
        for k, v in self.ENV.items():
            env_flags += ["-e", f"{k}={v}"]
        proc = subprocess.Popen(
            [
                "docker",
                "exec",
                "-i",
            ]
            + env_flags
            + [
                self.container_name,
                "gemini",
                "-p",
                "-",
                "--yolo",
                "-o",
                "stream-json",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(prompt)
        proc.stdin.close()

        stdout = proc.stdout
        output_lines: list[str] = []
        steps = 0
        reasoning_count = 0
        llm_calls = 0
        last_event_time = time.monotonic()
        timed_out = False

        # Track state to count reasoning turns
        in_assistant_message = False
        awaiting_llm = (
            True  # a model-output turn is pending (start, or after a tool result)
        )

        def read_output():
            nonlocal steps, reasoning_count, llm_calls, in_assistant_message, awaiting_llm, last_event_time
            for line in stdout:
                last_event_time = time.monotonic()
                if debug:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                output_lines.append(line)

                event_type, role = self._parse_event(line)
                # one LLM invocation per model-output turn (tool call or assistant
                # text); a tool result ends the turn so the next output is a new one.
                model_output = event_type == "tool_use" or (
                    event_type == "message" and role == "assistant"
                )
                if model_output and awaiting_llm:
                    llm_calls += 1
                    awaiting_llm = False
                    if count_llm_calls and over_budget(
                        self.container_name, llm_calls, max_steps
                    ):
                        return
                if event_type == "tool_use":
                    steps += 1
                    in_assistant_message = False
                    if not count_llm_calls and over_budget(
                        self.container_name, steps, max_steps
                    ):
                        return
                    if not debug:
                        sys.stdout.write(
                            f"\rStep {steps} | Reasoning {reasoning_count}   "
                        )
                        sys.stdout.flush()
                elif event_type == "message" and role == "assistant":
                    if not in_assistant_message:
                        reasoning_count += 1
                        in_assistant_message = True
                        if not debug:
                            sys.stdout.write(
                                f"\rStep {steps} | Reasoning {reasoning_count}   "
                            )
                            sys.stdout.flush()
                        if (
                            max_reasoning is not None
                            and reasoning_count >= max_reasoning
                        ):
                            return
                elif event_type == "tool_result":
                    in_assistant_message = False
                    awaiting_llm = True
                elif event_type == "result":
                    in_assistant_message = False
                    awaiting_llm = True

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()

        if max_tool_timeout is not None:
            while reader.is_alive():
                reader.join(timeout=5)
                if (
                    reader.is_alive()
                    and (time.monotonic() - last_event_time) > max_tool_timeout
                ):
                    timed_out = True
                    print(
                        f"\n[harness] Tool timeout: no output for {max_tool_timeout}s, killing agent."
                    )
                    break
        else:
            reader.join()

        if not debug:
            sys.stdout.write("\n")
            sys.stdout.flush()

        budget = llm_calls if count_llm_calls else steps
        hit_limit = (
            timed_out
            or (max_steps is not None and budget >= max_steps)
            or (max_reasoning is not None and reasoning_count >= max_reasoning)
        )
        if hit_limit:
            subprocess.run(
                ["docker", "kill", self.container_name],
                capture_output=True,
            )
            proc.kill()

        proc.wait()
        self.reasoning = "".join(output_lines)

        # save reasoning trace to container workspace (might fail if killed, but we have it on host)
        self._exec(
            ["sh", "-c", f"cat > {REASONING_PATH}"],
            input=self.reasoning,
            text=True,
            capture_output=True,
        )

        # extract last message from assistant's final response turn
        self.last_message = ""
        current_msg_chunks = []
        for line in output_lines:
            try:
                obj = json.loads(line)
                if obj.get("type") == "message" and obj.get("role") == "assistant":
                    current_msg_chunks.append(obj.get("content", ""))
                elif obj.get("type") == "tool_use":
                    current_msg_chunks = []
            except (json.JSONDecodeError, AttributeError):
                pass

        if current_msg_chunks:
            self.last_message = "".join(current_msg_chunks).strip()

        if self.last_message:
            self._exec(
                ["sh", "-c", f"cat > {RESULT_PATH}"],
                input=self.last_message,
                text=True,
                capture_output=True,
            )

        return self.reasoning, self.last_message, budget, reasoning_count

    def cleanup(self):
        """Save refreshed auth credentials back to host before container is destroyed."""
        for filename in ["google_accounts.json", "oauth_creds.json", "settings.json"]:
            subprocess.run(
                [
                    "docker",
                    "cp",
                    f"{self.container_name}:/root/.gemini/{filename}",
                    os.path.join(GEMINI_DIR, filename),
                ],
                capture_output=True,
            )

    def get_metrics(self):
        """Parse token usage from the reasoning trace captured during run."""
        if not self.reasoning:
            return None

        # look for the final "result" event which has cumulative usage
        tokens = None
        for line in self.reasoning.splitlines():
            try:
                obj = json.loads(line)
                if obj.get("type") != "result":
                    continue
                stats = obj.get("stats", {})

                # Gemini stats in stream-json result are cumulative
                tokens = {
                    "input_tokens": stats.get("input_tokens", 0),
                    "cached_input_tokens": stats.get("cached", 0),
                    "output_tokens": stats.get("output_tokens", 0),
                    "total_tokens": stats.get("total_tokens", 0),
                }
            except (json.JSONDecodeError, KeyError):
                pass

        return tokens

    @staticmethod
    def parse_reasoning(raw_text):
        """Parse Gemini stream-json reasoning trace into normalized events.

        Gemini streams assistant messages as consecutive delta fragments that
        need to be merged into single reasoning events.

        Returns list of dicts with keys: type, text, timestamp, metadata.
        """
        events = []
        pending_assistant_chunks = []
        pending_ts = None

        def flush_assistant():
            nonlocal pending_assistant_chunks, pending_ts
            if pending_assistant_chunks:
                events.append(
                    {
                        "type": "reasoning",
                        "text": "".join(pending_assistant_chunks),
                        "timestamp": pending_ts,
                        "metadata": {},
                    }
                )
                pending_assistant_chunks = []
                pending_ts = None

        for line in raw_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = obj.get("type")
            ts = obj.get("timestamp")

            if etype == "init":
                flush_assistant()
                events.append(
                    {
                        "type": "system",
                        "text": f"Session started — model: {obj.get('model', 'unknown')}",
                        "timestamp": ts,
                        "metadata": {
                            "subtype": "init",
                            "model": obj.get("model"),
                            "session_id": obj.get("session_id"),
                        },
                    }
                )

            elif etype == "message":
                role = obj.get("role")
                content = obj.get("content", "")
                if role == "assistant":
                    if pending_ts is None:
                        pending_ts = ts
                    pending_assistant_chunks.append(content)
                elif role == "user":
                    flush_assistant()
                    # Skip the initial prompt (it's very long and not useful)
                    pass

            elif etype == "tool_use":
                flush_assistant()
                tool_name = obj.get("tool_name", "unknown")
                params = obj.get("parameters", {})
                events.append(
                    {
                        "type": "tool_call",
                        "text": json.dumps(params, ensure_ascii=False),
                        "timestamp": ts,
                        "metadata": {
                            "tool_name": tool_name,
                            "tool_id": obj.get("tool_id"),
                        },
                    }
                )

            elif etype == "tool_result":
                flush_assistant()
                events.append(
                    {
                        "type": "tool_result",
                        "text": obj.get("output", ""),
                        "timestamp": ts,
                        "metadata": {
                            "tool_id": obj.get("tool_id"),
                            "status": obj.get("status"),
                        },
                    }
                )

            elif etype == "result":
                flush_assistant()
                stats = obj.get("stats", {})
                events.append(
                    {
                        "type": "system",
                        "text": f"Session ended — {obj.get('status', '')}",
                        "timestamp": ts,
                        "metadata": {
                            "subtype": "result",
                            "stats": stats,
                        },
                    }
                )

        flush_assistant()
        return events

    def invoke_judge(self, prompt, **kwargs):
        empty_dir = tempfile.mkdtemp(prefix="gemini-judge-dir-")

        try:
            result = subprocess.run(
                ["gemini", "--yolo", "-p", "-"],
                input=prompt,
                cwd=empty_dir,
                capture_output=True,
                text=True,
                timeout=500,
            )

            return result
        finally:
            if os.path.exists(empty_dir):
                shutil.rmtree(empty_dir)
