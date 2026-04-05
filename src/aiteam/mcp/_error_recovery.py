"""Error type → recovery strategy mapping for MCP API calls.

All tools using _api_call() automatically inherit these recovery hints.
No per-tool maintenance required.
"""

from __future__ import annotations

# HTTP status code → recovery strategy
ERROR_RECOVERY_MAP: dict[int | str, dict[str, str]] = {
    # ---- Client errors ----
    400: {
        "category": "bad_request",
        "recovery": "请求格式错误。检查参数名称和数据类型是否正确。",
    },
    401: {
        "category": "unauthorized",
        "recovery": "认证失败。请检查是否提供了有效的认证凭证。",
    },
    403: {
        "category": "forbidden",
        "recovery": "权限不足。该操作需要更高权限，请联系管理员。",
    },
    404: {
        "category": "resource_not_found",
        "recovery": "确认资源ID是否正确。尝试先用list工具查询可用资源，再重新操作。",
    },
    409: {
        "category": "conflict",
        "recovery": "资源状态冲突。先查询当前状态(task_status/team_status)，再决定下一步。",
    },
    422: {
        "category": "validation_error",
        "recovery": "参数验证失败。检查必填参数是否提供、格式是否正确。",
    },
    429: {
        "category": "rate_limited",
        "recovery": "请求频率过高。稍等片刻后重试。",
    },
    # ---- Server errors ----
    500: {
        "category": "server_error",
        "recovery": "服务器内部错误。等待3秒后重试一次。如果仍失败，使用os_health_check检查系统状态。",
    },
    502: {
        "category": "bad_gateway",
        "recovery": "上游服务不可用。使用os_health_check检查系统状态，稍后重试。",
    },
    503: {
        "category": "service_unavailable",
        "recovery": "服务暂时不可用。使用os_health_check检查系统状态，等待服务恢复后重试。",
    },
    # ---- Connection-level errors ----
    "connection_refused": {
        "category": "api_unavailable",
        "recovery": "API服务未运行。系统将自动启动，请稍后重试。",
    },
    "timeout": {
        "category": "timeout",
        "recovery": "请求超时。稍后重试，如果持续超时使用os_health_check诊断。",
    },
    "unknown": {
        "category": "unknown",
        "recovery": "未知错误。使用os_health_check检查系统状态，查看日志获取详细信息。",
    },
}

# Business-level error keywords in response body → error category
# Matched against lower-cased response body text (longest key wins on tie)
BUSINESS_ERROR_MAP: dict[str, str] = {
    "不存在": "resource_not_found",
    "not found": "resource_not_found",
    "already exists": "conflict",
    "已存在": "conflict",
    "已结束": "state_conflict",
    "permission": "permission_denied",
    "权限": "permission_denied",
    "invalid": "validation_error",
    "无效": "validation_error",
}


def get_http_recovery(status_code: int) -> dict[str, str]:
    """Return recovery info for an HTTP status code.

    Falls back to the generic server_error entry for 5xx codes, and
    an empty dict for codes with no mapping.
    """
    if status_code in ERROR_RECOVERY_MAP:
        return ERROR_RECOVERY_MAP[status_code]
    if status_code >= 500:
        return ERROR_RECOVERY_MAP[500]
    return {}


def get_connection_recovery(error_str: str) -> dict[str, str]:
    """Return recovery info for a connection-level error string."""
    lowered = error_str.lower()
    if "connection refused" in lowered or "connect" in lowered:
        return ERROR_RECOVERY_MAP["connection_refused"]
    if "timed out" in lowered or "timeout" in lowered:
        return ERROR_RECOVERY_MAP["timeout"]
    return ERROR_RECOVERY_MAP["unknown"]


def get_business_recovery(response_body: str) -> str:
    """Return error category string if any business keyword found in body.

    Returns empty string when no keyword matches.
    """
    lowered = response_body.lower()
    for keyword, category in BUSINESS_ERROR_MAP.items():
        if keyword.lower() in lowered:
            return category
    return ""
