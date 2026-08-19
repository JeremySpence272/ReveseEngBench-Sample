#!/usr/bin/env python3

"""Pretty-print Codex reasoning traces (reasoning.jsonl).

The trace produced by `codex exec --json` is expected to be JSONL, but this
project captures stdout+stderr together, so non-JSON log lines can appear.
This tool handles both.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Any

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"


LOG_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
ITEM_EVENT_RE = re.compile(r"^(?:item|response_item)\.")
LIFECYCLE_RE = re.compile(r"^(?:thread|turn|response)\.")


@dataclass
class RenderConfig:
    use_color: bool
    max_field: int
    max_block_lines: int
    show_json: bool


class Colors:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def wrap(self, code: str, text: str) -> str:
        if not self.enabled:
            return text
        return f"{code}{text}{ANSI_RESET}"

    def dim(self, text: str) -> str:
        return self.wrap(ANSI_DIM, text)

    def bold(self, text: str) -> str:
        return self.wrap(ANSI_BOLD, text)

    def red(self, text: str) -> str:
        return self.wrap(ANSI_RED, text)

    def green(self, text: str) -> str:
        return self.wrap(ANSI_GREEN, text)

    def yellow(self, text: str) -> str:
        return self.wrap(ANSI_YELLOW, text)

    def blue(self, text: str) -> str:
        return self.wrap(ANSI_BLUE, text)

    def magenta(self, text: str) -> str:
        return self.wrap(ANSI_MAGENTA, text)

    def cyan(self, text: str) -> str:
        return self.wrap(ANSI_CYAN, text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pretty-print Codex reasoning JSONL with colors and summaries."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="Path to reasoning JSONL file. If omitted, latest results/*_reasoning.jsonl is used. Use '-' for stdin.",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colors.",
    )
    parser.add_argument(
        "--max-field",
        type=int,
        default=240,
        help="Max characters per inline field before truncation (default: 240).",
    )
    parser.add_argument(
        "--max-block-lines",
        type=int,
        default=10,
        help="Max lines shown for multi-line blocks like function outputs (default: 10).",
    )
    parser.add_argument(
        "--show-json",
        action="store_true",
        help="Also print compact JSON for each parsed event.",
    )
    return parser.parse_args()


def should_use_color(no_color: bool) -> bool:
    if no_color:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def repo_root_from_script() -> str:
    here = os.path.abspath(os.path.dirname(__file__))
    return os.path.abspath(os.path.join(here, "..", ".."))


def resolve_input_path(path_arg: str | None) -> str:
    if path_arg:
        return path_arg
    root = repo_root_from_script()
    results_dir = os.path.join(root, "results")
    candidates: list[str] = []
    if os.path.isdir(results_dir):
        for name in os.listdir(results_dir):
            if name.endswith("_reasoning.jsonl"):
                candidates.append(os.path.join(results_dir, name))
    if not candidates:
        raise FileNotFoundError(
            "No input path provided and no results/*_reasoning.jsonl file found."
        )
    return max(candidates, key=os.path.getmtime)


def trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def one_line(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        raw = str(value)
    compact = " ".join(raw.split())
    return trim(compact, limit)


def block_text(value: Any, max_chars: int, max_lines: int) -> str:
    if value is None:
        return ""
    text = str(value)
    lines = text.splitlines() or [text]
    shown = lines[:max_lines]
    clipped_lines = len(lines) - len(shown)
    combined = "\n".join(shown)
    if len(combined) > max_chars:
        combined = trim(combined, max_chars)
    if clipped_lines > 0:
        combined += f"\n... ({clipped_lines} more lines)"
    return combined


def extract_mcp_result_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return str(result)

    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)

    structured = result.get("structured_content")
    if isinstance(structured, dict):
        if "result" in structured:
            parts.append(str(structured["result"]))
        elif structured:
            parts.append(json.dumps(structured, ensure_ascii=False))
    elif structured is not None:
        parts.append(str(structured))

    if not parts:
        return ""
    # De-duplicate while preserving order.
    seen: set[str] = set()
    deduped: list[str] = []
    for part in parts:
        if part in seen:
            continue
        seen.add(part)
        deduped.append(part)
    return "\n".join(deduped)


def extract_text_parts(node: Any) -> list[str]:
    if node is None:
        return []
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        out: list[str] = []
        for x in node:
            out.extend(extract_text_parts(x))
        return out
    if not isinstance(node, dict):
        return [str(node)]

    output: list[str] = []
    for key in ("text", "message", "output_text", "input_text"):
        val = node.get(key)
        if isinstance(val, str) and val:
            output.append(val)
    content = node.get("content")
    if isinstance(content, (list, dict, str)):
        output.extend(extract_text_parts(content))
    summary = node.get("summary")
    if isinstance(summary, list):
        output.extend(extract_text_parts(summary))
    return output


def short_ts(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return ""


def normalize_event(obj: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
    # Native `codex exec --json` format usually has top-level `type`.
    event_type = obj.get("type")
    ts = short_ts(obj.get("timestamp"))
    if isinstance(event_type, str):
        if event_type == "response_item" and isinstance(obj.get("payload"), dict):
            payload = obj["payload"]
            payload_type = payload.get("type", "unknown")
            return f"response_item.{payload_type}", payload, ts
        if event_type == "event_msg" and isinstance(obj.get("payload"), dict):
            payload = obj["payload"]
            payload_type = payload.get("type", "unknown")
            return f"event_msg.{payload_type}", payload, ts
        if event_type in {"session_meta", "turn_context"} and isinstance(
            obj.get("payload"), dict
        ):
            return event_type, obj["payload"], ts
        return event_type, obj, ts
    return "json.unknown", obj, ts


def event_color(colors: Colors, event_type: str) -> Any:
    if "failed" in event_type or event_type == "error":
        return colors.red
    if event_type.startswith("event_msg.agent_message"):
        return colors.green
    if event_type.startswith("event_msg.user_message"):
        return colors.yellow
    if ITEM_EVENT_RE.match(event_type):
        return colors.green
    if LIFECYCLE_RE.match(event_type):
        return colors.cyan
    if event_type.startswith("event_msg."):
        return colors.blue
    if event_type.startswith("session_") or event_type.startswith("turn_context"):
        return colors.magenta
    return colors.blue


def item_from_event(event_type: str, event: dict[str, Any]) -> dict[str, Any] | None:
    if event_type.startswith("item.") and isinstance(event.get("item"), dict):
        return event["item"]
    if event_type.startswith("response_item.") and isinstance(event, dict):
        return event
    return None


def render_item_details(
    item: dict[str, Any], cfg: RenderConfig, colors: Colors
) -> list[str]:
    out: list[str] = []
    item_type = str(item.get("type", "unknown"))
    item_id = item.get("id")
    role = item.get("role")

    header = f"item_type={item_type}"
    if item_id:
        header += f" id={item_id}"
    if role:
        header += f" role={role}"
    out.append(header)

    if item_type in {"message"}:
        txt = " ".join(extract_text_parts(item.get("content"))).strip()
        if txt:
            out.append(f"text={trim(' '.join(txt.split()), cfg.max_field)}")
    elif item_type in {"agent_message", "user_message", "system_message"}:
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            out.append(f"text={trim(' '.join(text.split()), cfg.max_field)}")
    elif item_type in {"reasoning"}:
        content = item.get("content")
        if content:
            out.append(f"content={one_line(content, cfg.max_field)}")
        summary_txt = " ".join(extract_text_parts(item.get("summary"))).strip()
        if summary_txt:
            out.append(f"summary={trim(' '.join(summary_txt.split()), cfg.max_field)}")
        encrypted = item.get("encrypted_content")
        if isinstance(encrypted, str):
            out.append(f"encrypted_content=<len={len(encrypted)}>")
            if not content and not summary_txt:
                out.append(
                    "raw_reasoning=not available (Codex provides encrypted reasoning content)"
                )
    elif item_type in {"function_call", "custom_tool_call"}:
        name = item.get("name") or item.get("tool") or "unknown"
        out.append(f"name={trim(str(name), cfg.max_field)}")
        args = item.get("arguments")
        if args is not None:
            out.append(f"arguments={one_line(args, cfg.max_field)}")
    elif item_type in {"function_call_output", "custom_tool_call_output"}:
        call_id = item.get("call_id")
        if call_id:
            out.append(f"call_id={call_id}")
        output = item.get("output")
        if output is not None:
            block = block_text(output, cfg.max_field * 2, cfg.max_block_lines)
            if block:
                out.append("output:")
                for line in block.splitlines():
                    out.append(f"  {line}")
    elif item_type in {"web_search_call"}:
        query = item.get("query") or item.get("input")
        if query:
            out.append(f"query={one_line(query, cfg.max_field)}")
        status = item.get("status")
        if status:
            out.append(f"status={status}")
    elif item_type in {"command_execution"}:
        command = item.get("command")
        status = item.get("status")
        exit_code = item.get("exit_code")
        if command:
            out.append(f"command={one_line(command, cfg.max_field)}")
        if status:
            out.append(f"status={status}")
        if exit_code is not None:
            out.append(f"exit_code={exit_code}")
        aggregated_output = item.get("aggregated_output")
        if aggregated_output:
            block = block_text(
                aggregated_output, cfg.max_field * 4, cfg.max_block_lines
            )
            if block:
                out.append("aggregated_output:")
                for line in block.splitlines():
                    out.append(f"  {line}")
    elif item_type in {"mcp_tool_call"}:
        server = item.get("server")
        tool = item.get("tool")
        status = item.get("status")
        if server:
            out.append(f"server={server}")
        if tool:
            out.append(f"tool={tool}")
        if status:
            out.append(f"status={status}")
        args = item.get("arguments")
        if args:
            out.append(f"arguments={one_line(args, cfg.max_field)}")
        err = item.get("error")
        if err:
            if isinstance(err, dict):
                msg = err.get("message") or one_line(err, cfg.max_field)
                out.append(f"error={one_line(msg, cfg.max_field * 2)}")
            else:
                out.append(f"error={one_line(err, cfg.max_field * 2)}")
        result_text = extract_mcp_result_text(item.get("result"))
        if result_text:
            block = block_text(result_text, cfg.max_field * 4, cfg.max_block_lines)
            if block:
                out.append("result:")
                for line in block.splitlines():
                    out.append(f"  {line}")
    else:
        # Fallback: render keys likely useful for debugging without dumping huge JSON.
        interesting = {}
        for k in (
            "name",
            "text",
            "command",
            "server",
            "tool",
            "call_id",
            "status",
            "message",
            "error",
            "arguments",
            "aggregated_output",
            "output",
        ):
            if k in item:
                interesting[k] = item[k]
        if interesting:
            for k, v in interesting.items():
                if k in {"output", "aggregated_output"}:
                    block = block_text(v, cfg.max_field * 2, cfg.max_block_lines)
                    out.append(f"{k}:")
                    for line in block.splitlines():
                        out.append(f"  {line}")
                else:
                    out.append(f"{k}={one_line(v, cfg.max_field)}")

    # Add a dim fallback note when no details could be extracted.
    if len(out) == 1:
        out.append(colors.dim("(no additional item fields)"))
    return out


def render_event_line(
    line_no: int,
    event_type: str,
    event: dict[str, Any],
    ts: str,
    cfg: RenderConfig,
    colors: Colors,
) -> list[str]:
    color_fn = event_color(colors, event_type)
    width = max(4, len(str(line_no)))
    prefix = colors.dim(str(line_no).rjust(width))
    time_part = colors.dim(ts) + " " if ts else ""
    type_part = color_fn(event_type.upper())

    lines: list[str] = [f"{prefix} {time_part}{type_part}"]

    msg = None
    if "message" in event and isinstance(event["message"], str):
        msg = event["message"]
    elif "error" in event and isinstance(event["error"], dict):
        msg = event["error"].get("message")
    if msg:
        lines.append(f"{' ' * width}   {trim(msg, cfg.max_field * 2)}")

    # Event-specific quick details.
    if event_type == "thread.started":
        if event.get("thread_id"):
            lines.append(f"{' ' * width}   thread_id={event['thread_id']}")
    elif event_type == "turn.failed":
        err = event.get("error")
        if isinstance(err, dict) and err.get("message"):
            lines.append(
                f"{' ' * width}   error={trim(str(err['message']), cfg.max_field)}"
            )
    elif event_type == "turn.completed":
        usage = event.get("usage")
        if isinstance(usage, dict):
            lines.append(
                f"{' ' * width}   usage(in={usage.get('input_tokens', 0)}, out={usage.get('output_tokens', 0)}, total={usage.get('total_tokens', 0)})"
            )

    # Item event family.
    item = item_from_event(event_type, event)
    if item is not None:
        for detail in render_item_details(item, cfg, colors):
            lines.append(f"{' ' * width}   {detail}")

    if event_type.startswith("event_msg.token_count"):
        info = event.get("info", {})
        total = info.get("total_token_usage", {}) if isinstance(info, dict) else {}
        if isinstance(total, dict):
            lines.append(
                f"{' ' * width}   tokens(in={total.get('input_tokens', 0)}, out={total.get('output_tokens', 0)}, total={total.get('total_tokens', 0)})"
            )

    if cfg.show_json:
        compact = trim(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")),
            cfg.max_field * 3,
        )
        lines.append(f"{' ' * width}   {colors.dim('json=')}{compact}")

    return lines


def print_header(path: str, colors: Colors) -> None:
    term_w = shutil.get_terminal_size((120, 20)).columns
    title = f" Codex Reasoning Trace: {path} "
    bar = "=" * max(8, term_w)
    print(colors.bold(colors.blue(bar)))
    print(colors.bold(title))
    print(colors.bold(colors.blue(bar)))


def print_summary(
    stats: Counter[str],
    event_counts: Counter[str],
    item_counts: Counter[str],
    colors: Colors,
) -> None:
    print()
    print(colors.bold("Summary"))
    print(f"  lines: {stats['lines']}")
    print(f"  parsed_json: {stats['json']}")
    print(f"  non_json: {stats['non_json']}")
    print(f"  malformed_json: {stats['malformed_json']}")

    if event_counts:
        print("  event_types:")
        for etype, count in event_counts.most_common():
            print(f"    - {etype}: {count}")
    if item_counts:
        print("  item_types:")
        for itype, count in item_counts.most_common():
            print(f"    - {itype}: {count}")


def iter_lines(path: str):
    if path == "-":
        for line in sys.stdin:
            yield line
        return
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            yield line


def main() -> int:
    args = parse_args()
    try:
        path = resolve_input_path(args.path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    use_color = should_use_color(args.no_color)
    colors = Colors(use_color)
    cfg = RenderConfig(
        use_color=use_color,
        max_field=max(40, args.max_field),
        max_block_lines=max(1, args.max_block_lines),
        show_json=args.show_json,
    )

    if path != "-" and not os.path.exists(path):
        print(f"error: no such file: {path}", file=sys.stderr)
        return 2

    print_header(path, colors)

    stats: Counter[str] = Counter()
    event_counts: Counter[str] = Counter()
    item_counts: Counter[str] = Counter()

    try:
        for idx, raw in enumerate(iter_lines(path), start=1):
            stats["lines"] += 1
            line = raw.rstrip("\n")
            stripped = line.strip()
            if not stripped:
                continue

            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                stats["non_json"] += 1
                if stripped.startswith("{"):
                    stats["malformed_json"] += 1
                width = max(4, len(str(idx)))
                prefix = colors.dim(str(idx).rjust(width))
                tag = colors.yellow(
                    "RAW_LOG" if LOG_PREFIX_RE.match(stripped) else "RAW"
                )
                print(f"{prefix} {tag} {trim(stripped, cfg.max_field * 2)}")
                continue

            stats["json"] += 1
            if not isinstance(payload, dict):
                width = max(4, len(str(idx)))
                prefix = colors.dim(str(idx).rjust(width))
                print(
                    f"{prefix} {colors.magenta('JSON')} {trim(str(payload), cfg.max_field)}"
                )
                continue

            event_type, event_obj, ts = normalize_event(payload)
            event_counts[event_type] += 1

            item = item_from_event(event_type, event_obj)
            if item is not None:
                item_type = item.get("type")
                if isinstance(item_type, str):
                    item_counts[item_type] += 1

            for out_line in render_event_line(
                line_no=idx,
                event_type=event_type,
                event=event_obj,
                ts=ts,
                cfg=cfg,
                colors=colors,
            ):
                print(out_line)
    except OSError as exc:
        print(f"error: unable to read '{path}': {exc}", file=sys.stderr)
        return 2

    print_summary(stats, event_counts, item_counts, colors)
    return 0


if __name__ == "__main__":
    sys.exit(main())
