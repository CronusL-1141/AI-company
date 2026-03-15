#!/usr/bin/env python3
"""Claude Code StatusLine Script (Python, cross-platform)
Displays: [Model] Ctx:XX% | Total:XXK | $X.XX | ~/path/to/dir
Writes context monitor JSON for agent self-awareness.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path


def fix_json_backslashes(s: str) -> str:
    """Fix bare backslashes in Windows paths that break JSON parsing.

    Walk char by char: keep valid JSON escapes (\\", \\\\, \\/, \\b, \\f, \\n, \\r, \\t, \\u),
    double all others so they become literal backslashes.
    """
    valid_esc = set('"\\/bfnrtu')
    out = []
    i = 0
    while i < len(s):
        if s[i] == "\\":
            if (i + 1) < len(s) and s[i + 1] in valid_esc:
                out.append(s[i])
                i += 1
                out.append(s[i])
            else:
                out.append("\\\\")
        else:
            out.append(s[i])
        i += 1
    return "".join(out)


def main():
    try:
        input_data = sys.stdin.read()
        if not input_data or not input_data.strip():
            sys.stdout.write("[Claude] No data")
            return

        # Try parsing directly first; fall back to backslash repair for Windows paths
        try:
            data = json.loads(input_data)
        except json.JSONDecodeError:
            data = json.loads(fix_json_backslashes(input_data))

        # === Context window usage ===
        ctx = data.get("context_window", {})
        used_pct = None
        if ctx:
            if ctx.get("used_percentage") is not None:
                used_pct = round(float(ctx["used_percentage"]), 1)
            elif ctx.get("current_usage") and ctx.get("context_window_size", 0) > 0:
                usage = ctx["current_usage"]
                current_tokens = (
                    int(usage.get("input_tokens", 0))
                    + int(usage.get("cache_creation_input_tokens", 0))
                    + int(usage.get("cache_read_input_tokens", 0))
                )
                used_pct = round(current_tokens * 100.0 / int(ctx["context_window_size"]), 1)

        # === Project cumulative tokens (parse JSONL files) ===
        total_project_input = 0
        total_project_output = 0

        project_dir_raw = data.get("workspace", {}).get("project_dir", "")
        if project_dir_raw:
            # Claude stores project dirs by replacing non-alphanumeric chars with '-'
            # Try exact slug first, then parent dirs (CC may use working dir root, not subdirectory)
            projects_base = Path.home() / ".claude" / "projects"
            claude_project_path = None
            check_path = project_dir_raw.replace("\\", "/")
            while check_path and check_path != "/":
                slug = re.sub(r"[^a-zA-Z0-9]", "-", check_path)
                candidate = projects_base / slug
                if candidate.is_dir():
                    claude_project_path = candidate
                    break
                # Try parent
                check_path = check_path.rsplit("/", 1)[0] if "/" in check_path else ""

            if claude_project_path.is_dir():
                for jsonl_file in claude_project_path.glob("*.jsonl"):
                    if jsonl_file.name.startswith("agent-"):
                        continue
                    try:
                        with open(jsonl_file, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    entry = json.loads(line)
                                    u = (entry.get("message") or {}).get("usage")
                                    if u:
                                        total_project_input += int(u.get("input_tokens", 0))
                                        total_project_input += int(u.get("cache_creation_input_tokens", 0))
                                        total_project_input += int(u.get("cache_read_input_tokens", 0))
                                        total_project_output += int(u.get("output_tokens", 0))
                                except (json.JSONDecodeError, ValueError, TypeError):
                                    pass
                    except OSError:
                        pass

        # === Calculate project cost ===
        model_id = data.get("model", {}).get("id", "")
        if "opus" in model_id:
            input_rate, output_rate = 15, 75
        elif "haiku" in model_id:
            input_rate, output_rate = 0.8, 4
        else:
            input_rate, output_rate = 3, 15

        project_cost = round(
            (total_project_input * input_rate / 1_000_000)
            + (total_project_output * output_rate / 1_000_000),
            2,
        )

        # === Format token display ===
        total_tokens = total_project_input + total_project_output
        if total_tokens >= 1_000_000:
            token_display = f"{total_tokens / 1_000_000:.1f}M"
        elif total_tokens >= 1000:
            token_display = f"{total_tokens / 1000:.1f}K"
        else:
            token_display = str(total_tokens)

        # === Current directory (shorten home to ~) ===
        cwd_raw = data.get("cwd") or data.get("workspace", {}).get("current_dir", "")
        cwd_display = ""
        if cwd_raw:
            home_path = str(Path.home()).replace("\\", "/")
            cwd_norm = cwd_raw.replace("\\", "/")
            if cwd_norm.lower().startswith(home_path.lower()):
                cwd_display = "~" + cwd_norm[len(home_path):]
            else:
                cwd_display = cwd_norm

        # === Assemble output ===
        model_name = data.get("model", {}).get("display_name", "Claude")
        cost_display = f"{project_cost:.2f}"

        parts = [f"[{model_name}]"]
        if used_pct is not None:
            parts.append(f"Ctx:{used_pct:.1f}%")
        parts.append(f"Total:{token_display}")
        parts.append(f"${cost_display}")
        if cwd_display:
            parts.append(cwd_display)

        sys.stdout.write(" | ".join(parts))

        # === Write context monitor file for agent self-awareness ===
        if used_pct is not None:
            try:
                monitor_path = Path.home() / ".claude" / "context-monitor.json"
                monitor_data = {
                    "used_percentage": used_pct,
                    "context_window_size": int(ctx.get("context_window_size", 200000)),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
                monitor_path.write_text(
                    json.dumps(monitor_data, separators=(",", ":")),
                    encoding="utf-8",
                )
            except OSError:
                pass

    except Exception as e:
        sys.stdout.write(f"[StatusLine Error: {e}]")


if __name__ == "__main__":
    main()
