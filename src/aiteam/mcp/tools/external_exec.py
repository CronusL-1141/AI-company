"""Only read-only external-exec discovery is exposed to MCP."""

import json
from urllib.parse import urlencode

from aiteam.mcp._base import _api_call


def register(mcp):
    @mcp.tool()
    def usage_reconciliation(
        project_id: str = "",
        source_namespace: str = "",
        since: str = "",
        until: str = "",
        limit: int = 500,
    ) -> dict:
        """按需对账保存的 Codex journal、登记 agent 与外部执行；只读。

        输出各来源分母、未登记线程、结束无用量、计价缺口和读取截断。
        不调用模型、不采账户、不扫描认证；缺失用量未知，历史无证据 ephemeral 不可恢复。
        project_id 只筛登记/外部关联，journal 项目归属未知。since/until须含时区。

        Args:
            project_id: 明确项目 UUID；空串查询所有已登记执行，journal 项目归属仍未知。
            source_namespace: Codex home 的 SHA256 namespace；空串查询全部已保存来源。
            since: 含时区的 ISO 起始时间；空串不限制起点。
            until: 含时区的 ISO 结束时间；空串不限制终点。
            limit: 每种来源最大扫描样本数，范围 1 到 500；返回摘要及每桶最多 3 例，详情走 HTTP 分页。
        """
        parameters = {"limit": max(1, min(limit, 500)), "summary": "true", "bucket_limit": 3}
        for name, value in (
            ("project_id", project_id),
            ("source_namespace", source_namespace),
            ("since", since),
            ("until", until),
        ):
            if value:
                parameters[name] = value
        result = _api_call("GET", f"/api/external-execs/reconciliation?{urlencode(parameters)}")
        # Enforce a harness-facing byte bound even against an older API ignoring
        # summary. Do not return arbitrary API buckets or unbounded error bodies.
        if not isinstance(result, dict) or result.get("success") is not True:
            return {"success": False, "error": "reconciliation_unavailable"}
        data = result.get("data")
        if not isinstance(data, dict):
            return {"success": False, "error": "reconciliation_unavailable"}
        allowed = {
            "observed_at",
            "scope",
            "counts",
            "buckets",
            "bucket_counts",
            "bucket_truncated",
            "bucket_offset",
            "bucket_limit",
            "sample_limit",
            "summary",
            "truncated",
            "journal_status",
            "statements",
        }
        data = {key: value for key, value in data.items() if key in allowed}
        original = data.get("buckets", {})
        data["buckets"] = {key: items[:3] for key, items in original.items() if isinstance(items, list)}
        previous_flags = data.get("bucket_truncated", {})
        totals = data.get("bucket_counts", {})
        data["bucket_truncated"] = {
            key: bool(previous_flags.get(key)) or len(items) > 3 or totals.get(key, len(items)) > len(items[:3])
            for key, items in original.items()
            if isinstance(items, list)
        }
        data["summary"] = True
        result = {"success": True, "data": data}
        if len(json.dumps(result, ensure_ascii=False).encode()) > 16384:
            data["buckets"] = {}
            data["bucket_truncated"] = {key: bool(count) for key, count in data.get("bucket_counts", {}).items()}
            data["statements"] = ["MCP摘要达到大小预算，详细桶请使用HTTP分页。"]
        if len(json.dumps(result, ensure_ascii=False).encode()) > 16384:
            return {"success": False, "error": "reconciliation_summary_exceeds_budget"}
        return result
