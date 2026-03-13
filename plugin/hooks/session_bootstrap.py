#!/usr/bin/env python3
"""AI Team OS — Session启动引导脚本

SessionStart hook触发时首先执行，检测OS API是否可达。
如果不可达，提示用户运行 /os-up 启动服务。

只使用Python标准库。
"""

import json
import sys
import urllib.error
import urllib.request

API_URL = "http://localhost:8000"
HEALTH_ENDPOINT = f"{API_URL}/api/teams"


def main() -> None:
    # 读取stdin中的session信息
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
        session_info = json.loads(raw) if raw.strip() else {}
    except Exception:
        session_info = {}

    # 检测API是否可达
    try:
        req = urllib.request.Request(HEALTH_ENDPOINT, method="GET")
        with urllib.request.urlopen(req, timeout=1) as resp:
            resp.read()

        # API可达，确认到stderr（不干扰CC stdout）
        sys.stderr.write(
            f"[aiteam-bootstrap] AI Team OS API reachable at {API_URL}\n"
            f"[aiteam-bootstrap] session_id={session_info.get('session_id', 'unknown')}\n"
        )

    except (urllib.error.URLError, OSError) as e:
        # API不可达，警告用户
        sys.stderr.write(
            f"[aiteam-bootstrap] AI Team OS API not reachable at {API_URL}\n"
            f"[aiteam-bootstrap] Run /os-up to start the services.\n"
            f"[aiteam-bootstrap] Error: {e}\n"
        )
    except Exception:
        # 静默处理所有异常，不阻塞CC启动
        pass


if __name__ == "__main__":
    main()
