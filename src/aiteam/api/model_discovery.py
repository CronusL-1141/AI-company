"""模型发现与默认模型配置 — 模型治理（docs/model-governance-design.md）。

可用模型 = 文件真相源自动拉取：扫本机全部 CC transcript 实际出现过的
message.model（你真实用过的模型就是你可用的模型），零 API 依赖、零硬编码
清单（硬编码清单收不到 deepreasoning-coding-max-4.7 这类第三方接入——实测存在）。

默认启动模型写 ~/.claude/settings.json 顶层 "model" 键（CC 官方支持，新会话
生效）。settings.json 是用户全局配置，写保护三层：只动 model 一个键 /
写前备份 / tmp+rename 原子写。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

_MODEL_RE = re.compile(rb'"model"\s*:\s*"([^"]{2,80})"')
_TAIL_BYTES = 100_000
_SYNTHETIC = "<synthetic>"
# 纯层级别名（非完整模型 ID）——标注 alias，不进默认下拉主列表
_ALIASES = {"opus", "sonnet", "haiku", "fable", "default"}

_cache: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 60.0


def _projects_dir() -> Path:
    return Path.home() / ".claude" / "projects"


def _settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def scan_available_models(force: bool = False) -> list[dict]:
    """扫描全部主会话 transcript，聚合出现过的模型。

    返回 [{model, file_count, last_seen_ts, alias}]，按 last_seen 降序。
    109 文件实测 ~1s；60s 进程内缓存。
    """
    now = time.time()
    if not force and _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return _cache["data"]

    stats: dict[str, dict] = {}
    root = _projects_dir()
    try:
        slug_dirs = [d for d in root.iterdir() if d.is_dir()]
    except OSError:
        return []
    for slug in slug_dirs:
        try:
            files = [f for f in slug.glob("*.jsonl") if f.stat().st_size > 1024]
        except OSError:
            continue
        for f in files:
            try:
                st = f.stat()
                with open(f, "rb") as fh:
                    if st.st_size > _TAIL_BYTES:
                        fh.seek(st.st_size - _TAIL_BYTES)
                    data = fh.read()
            except OSError:
                continue
            seen_here: set[str] = set()
            for m in _MODEL_RE.finditer(data):
                try:
                    model = m.group(1).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                if model == _SYNTHETIC or model in seen_here:
                    continue
                seen_here.add(model)
                entry = stats.setdefault(
                    model, {"model": model, "file_count": 0, "last_seen_ts": 0.0}
                )
                entry["file_count"] += 1
                entry["last_seen_ts"] = max(entry["last_seen_ts"], st.st_mtime)

    result = sorted(stats.values(), key=lambda x: x["last_seen_ts"], reverse=True)
    for r in result:
        r["alias"] = r["model"] in _ALIASES
    _cache["ts"] = now
    _cache["data"] = result
    return result


def read_default_model() -> str:
    """读 settings.json 顶层 model 键（未设置返回空串）。"""
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            return str(json.load(f).get("model") or "")
    except Exception:  # noqa: BLE001
        return ""


def set_default_model(model: str) -> dict:
    """写 settings.json 顶层 model 键（空串=移除，恢复 CC 自身默认）。

    三层写保护：只动 model 键 / 写前备份 .bak-aiteam / tmp+rename 原子写。
    """
    model = (model or "").strip()
    sp = _settings_path()
    try:
        with open(sp, encoding="utf-8") as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"settings.json 解析失败，拒绝写入: {exc}"}

    # 备份（best-effort）
    try:
        if sp.exists():
            backup = sp.with_name("settings.json.bak-aiteam")
            backup.write_bytes(sp.read_bytes())
    except OSError:
        pass

    prev = settings.get("model")
    if model:
        settings["model"] = model
    else:
        settings.pop("model", None)

    tmp = sp.with_name("settings.json.tmp-aiteam")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, sp)
    except OSError as exc:
        return {"ok": False, "error": f"写入失败: {exc}"}
    return {"ok": True, "previous": prev or "", "current": model}
