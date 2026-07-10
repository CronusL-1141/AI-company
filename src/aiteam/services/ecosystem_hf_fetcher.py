"""HuggingFace Spaces fetcher — 多源生态档案补充。

设计哲学（v1.6.1）：
- HF Space 跟 GitHub repo 是**平行项目托管平台**，不是分发渠道
- 同项目跨源时**合并到一个 profile**（sources 多源列表），不创建两份独立行
- 通过 HF 公开 HTTP API（无需 token / SDK）抓取

接入流程：
    1. list_spaces(filter_tags=...) → 拿候选列表
    2. get_space_detail(id) → 取 cardData + tags
    3. extract_linked_github(readme) → 解析 GitHub URL（README 文本搜索）
    4. merge_or_create_profile(repo, space, gh_url):
       - 有 gh_url 且 github profile 存在 → append source 到现有
       - 有 gh_url 但 github profile 不存在 → 反查 gh API 建 github profile + 双源
       - 无 gh_url → 建独立 hf_space profile

API endpoints（公开，无需认证）：
- GET https://huggingface.co/api/spaces?search=...&sort=likes&limit=N
- GET https://huggingface.co/api/spaces/{owner}/{space}
- GET https://huggingface.co/{owner}/{space}/raw/main/README.md
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


HF_API_BASE = "https://huggingface.co/api"
HF_HUB_BASE = "https://huggingface.co"


# 链接到 GitHub repo 的正则（兼容 https/http、有无 trailing slash / .git）
_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?(?:/|$|\s|\")",
    re.IGNORECASE,
)


# Claude/Agent/MCP 生态相关的 HF tag 白名单（用于初步筛选，降低噪音）
DEFAULT_FILTER_TAGS = (
    "agent",
    "agents",
    "mcp",
    "mcp-server",
    "mcp-client",
    "claude",
    "claude-code",
    "anthropic",
    "llm-agent",
    "autonomous",
    "multi-agent",
)


@dataclass
class HfSpaceCandidate:
    """单个 HF Space 候选记录（从 list API 拿到的轻量元数据）。"""

    space_id: str  # "owner/space"
    likes: int
    sdk: str | None
    tags: list[str]
    private: bool
    created_at: str | None = None

    @property
    def owner(self) -> str:
        return self.space_id.split("/", 1)[0] if "/" in self.space_id else ""

    @property
    def name(self) -> str:
        return self.space_id.split("/", 1)[1] if "/" in self.space_id else self.space_id


@dataclass
class HfSpaceDetail:
    """完整的 HF Space 详情（含 cardData + linked github URL）。"""

    space_id: str
    likes: int
    sdk: str | None
    tags: list[str]
    author: str
    short_description: str
    last_modified: datetime | None
    created_at: datetime | None
    gated: bool
    disabled: bool
    private: bool
    host: str | None  # demo url
    linked_github: str | None = None  # "owner/repo" if found in README
    readme_excerpt: str = ""  # 前 400 字
    raw: dict[str, Any] = field(default_factory=dict)


def _http_get(url: str, timeout: float = 15.0) -> str:
    """GET 文本（用 urllib 避免额外依赖）。"""
    req = Request(url, headers={"User-Agent": "ai-team-os/v1.6.1 ecosystem-hf-fetcher"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_get_json(url: str, timeout: float = 15.0) -> Any:
    return json.loads(_http_get(url, timeout=timeout))


def _parse_iso8601(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # HF 返回 "2025-06-10T23:16:21.000Z" 格式
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def extract_linked_github(readme_text: str) -> str | None:
    """从 README 文本中提取首个 github.com/owner/repo 链接，返回 "owner/repo"。

    跳过 huggingface 自家无关链接、anchor、user-only URL。
    """
    if not readme_text:
        return None
    for m in _GITHUB_URL_RE.finditer(readme_text):
        owner, repo = m.group(1), m.group(2)
        # 过滤明显不是 repo 的（如 github.com/sponsors/...）
        if owner.lower() in {"sponsors", "issues", "marketplace", "settings"}:
            continue
        # 过滤 awesome-list 自我引用噪音（owner+repo 都太短或非 ASCII 异常）
        if len(repo) < 2:
            continue
        return f"{owner}/{repo}"
    return None


# ============================================================
# 公开 API
# ============================================================


def list_spaces_by_search(
    search: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
    sort: str = "likes",
) -> list[HfSpaceCandidate]:
    """调用 HF list API 拿候选列表。

    HF API 不支持 OR 多 tag 单次查询，需要每个 tag 单独请求再合并去重。
    """
    seen_ids: set[str] = set()
    results: list[HfSpaceCandidate] = []
    queries: list[str] = []
    if search:
        queries.append(f"search={quote(search)}")
    if tags:
        for tag in tags:
            queries.append(f"tags={quote(tag)}")
    if not queries:
        queries.append("")  # all

    for q in queries:
        url = f"{HF_API_BASE}/spaces?{q}&limit={limit}&sort={sort}&direction=-1"
        try:
            data = _http_get_json(url)
        except Exception as e:
            logger.warning("HF list spaces fail q=%s: %s", q, e)
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            sid = entry.get("id")
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            results.append(
                HfSpaceCandidate(
                    space_id=sid,
                    likes=entry.get("likes", 0),
                    sdk=entry.get("sdk"),
                    tags=list(entry.get("tags") or []),
                    private=bool(entry.get("private", False)),
                    created_at=entry.get("createdAt"),
                )
            )
    # 按 likes 降序排
    results.sort(key=lambda c: c.likes, reverse=True)
    return results


def get_space_detail(space_id: str, fetch_readme: bool = True) -> HfSpaceDetail | None:
    """拿单个 Space 的详情 + 可选 README 解析 github 链接。"""
    try:
        d = _http_get_json(f"{HF_API_BASE}/spaces/{space_id}")
    except Exception as e:
        logger.warning("HF detail fetch fail %s: %s", space_id, e)
        return None

    if not isinstance(d, dict):
        return None

    card = d.get("cardData") or {}
    readme_excerpt = ""
    linked_gh: str | None = None
    if fetch_readme:
        try:
            readme = _http_get(f"{HF_HUB_BASE}/{space_id}/raw/main/README.md", timeout=10.0)
            readme_excerpt = readme[:400]
            linked_gh = extract_linked_github(readme)
        except Exception:
            # README 可能不存在，不视为错误
            pass

    return HfSpaceDetail(
        space_id=d.get("id", space_id),
        likes=d.get("likes", 0),
        sdk=d.get("sdk") or card.get("sdk"),
        tags=list(d.get("tags") or []),
        author=d.get("author", space_id.split("/", 1)[0] if "/" in space_id else ""),
        short_description=card.get("short_description", "") or "",
        last_modified=_parse_iso8601(d.get("lastModified")),
        created_at=_parse_iso8601(d.get("createdAt")),
        gated=bool(d.get("gated", False)),
        disabled=bool(d.get("disabled", False)),
        private=bool(d.get("private", False)),
        host=d.get("host"),
        linked_github=linked_gh,
        readme_excerpt=readme_excerpt,
        raw=d,
    )


def build_hf_source_entry(detail: HfSpaceDetail) -> dict[str, Any]:
    """构造 sources 列表中的一个条目（HF Space 视角）。"""
    return {
        "kind": "hf_space",
        "id": detail.space_id,
        "likes": detail.likes,
        "url": f"{HF_HUB_BASE}/spaces/{detail.space_id}",
        "demo_url": detail.host,
        "sdk": detail.sdk,
        "last_seen_at": datetime.now(tz=UTC).isoformat(),
    }


def build_github_source_entry(repo_full_name: str, stars: int = 0) -> dict[str, Any]:
    """构造 sources 列表中的一个条目（GitHub 视角，HF 关联到的）。"""
    return {
        "kind": "github",
        "id": repo_full_name,
        "stars": stars,
        "url": f"https://github.com/{repo_full_name}",
        "last_seen_at": datetime.now(tz=UTC).isoformat(),
    }


# ============================================================
# Dry-run / PoC helper
# ============================================================


def dry_run_scan(
    search: str | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """探索性扫描，返回候选 + 链接分析，不写库。

    用于 PoC #49 验证去重逻辑能否有效命中 github 项目。
    """
    candidates = list_spaces_by_search(search=search, tags=tags, limit=limit)
    details: list[HfSpaceDetail] = []
    for c in candidates[:limit]:
        d = get_space_detail(c.space_id)
        if d:
            details.append(d)

    linked = [d for d in details if d.linked_github]
    isolated = [d for d in details if not d.linked_github]
    return {
        "total_candidates": len(candidates),
        "fetched_detail": len(details),
        "with_github_link": len(linked),
        "isolated_hf_only": len(isolated),
        "linked_pairs": [
            {"hf": d.space_id, "github": d.linked_github, "likes": d.likes, "sdk": d.sdk}
            for d in linked
        ],
        "isolated_samples": [
            {"hf": d.space_id, "likes": d.likes, "sdk": d.sdk, "desc": d.short_description[:80]}
            for d in isolated[:10]
        ],
    }


if __name__ == "__main__":
    # 单文件 PoC 入口
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "dry_run":
        result = dry_run_scan(tags=["mcp", "agent"], limit=15)
        print(json.dumps(result, indent=2, ensure_ascii=False))
