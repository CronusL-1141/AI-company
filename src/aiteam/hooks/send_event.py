#!/usr/bin/env python3
"""AI Team OS — Claude Code Hook事件发送器

CC hook触发时执行此脚本，将事件转发到OS API。
用法: python send_event.py <EventType> (从stdin读取JSON)

注意: 此脚本只使用Python标准库，不依赖任何第三方包，
因为它可能在任何Python环境中被CC直接调用。
"""

import json
import os
import sys
import urllib.error
import urllib.request

API_URL = os.environ.get("AITEAM_API_URL", "http://localhost:8000")

# 大字段截断限制（防止SubagentStop等事件payload过大导致超时）
MAX_FIELD_LEN = 500
LARGE_FIELDS = {"last_assistant_message", "agent_transcript_path", "transcript_path"}


def _trim_payload(payload: dict) -> dict:
    """截断过大的字段，防止HTTP超时。"""
    trimmed = {}
    for k, v in payload.items():
        if k in LARGE_FIELDS:
            if isinstance(v, str) and len(v) > MAX_FIELD_LEN:
                trimmed[k] = v[:MAX_FIELD_LEN] + "...(truncated)"
            elif isinstance(v, dict):
                trimmed[k] = str(v)[:MAX_FIELD_LEN] + "...(truncated)"
            else:
                trimmed[k] = v
        elif k == "tool_response" and isinstance(v, dict):
            # 截断工具输出但保留结构
            tr = {}
            for rk, rv in v.items():
                if isinstance(rv, str) and len(rv) > MAX_FIELD_LEN:
                    tr[rk] = rv[:MAX_FIELD_LEN] + "...(truncated)"
                else:
                    tr[rk] = rv
            trimmed[k] = tr
        else:
            trimmed[k] = v
    return trimmed


def main() -> None:
    try:
        # Windows下stdin默认用GBK解码，CC发送的是UTF-8，强制用buffer读取
        raw = sys.stdin.buffer.read().decode("utf-8")
        if not raw.strip():
            return

        payload = json.loads(raw)

        # CC hook payload不自带事件类型名，通过命令行参数注入
        if len(sys.argv) > 1 and "hook_event_name" not in payload:
            payload["hook_event_name"] = sys.argv[1]

        # 截断大字段
        payload = _trim_payload(payload)

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{API_URL}/api/hooks/event",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode())
            # 返回决策给CC（对于PreToolUse等需要决策的hook）
            if "decision" in result:
                print(json.dumps(result))

    except urllib.error.URLError:
        # OS服务未启动，静默忽略不阻塞CC
        pass
    except Exception:
        # 任何错误都不应阻塞CC运行
        pass


if __name__ == "__main__":
    main()
