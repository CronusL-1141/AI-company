"""批 3 ⑤：project_create 的 root_path 校验放行了不该放行的目录。

两条独立的漏洞，都冲撞「归属铁律：session 启动目录为准」：

1. ``cwd.startswith(given)`` 是裸前缀比较，没有分隔符边界：cwd=
   /Users/cronus/Desktop/AI team OS 时 root_path='/Users/cron' 也能过关
   （半个路径段）。修法照抄 _base.py 的 ``cwd == rp or cwd.startswith(rp + "/")``。
2. 分隔符对齐后 '/Users' 仍是 cwd 的合法祖先，照样能过关 —— 而把家目录的祖先
   注册成项目根，等于用一个项目吞掉整台机器（此后任何未注册目录都被它前缀
   认领）。故追加一条：项目根不得是家目录的**严格祖先**。
"""

from __future__ import annotations

from unittest.mock import patch

import aiteam.mcp.tools.project as project_tools

CWD = "/Users/cronus/Desktop/AI team OS"


class _ToolCapture:
    def __init__(self):
        self.tools: dict = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _project_create():
    cap = _ToolCapture()
    project_tools.register(cap)
    return cap.tools["project_create"]


def _call(root_path: str, cwd: str = CWD):
    tool = _project_create()
    with patch.object(project_tools, "_api_call", return_value={"success": True}) as api, \
         patch("os.getcwd", return_value=cwd):
        result = tool("demo", "", root_path)
    return result, api


class TestRootPathBoundary:
    def test_partial_segment_rejected(self):
        """半个路径段（/Users/cron 之于 /Users/cronus）—— 旧实现放行。"""
        result, api = _call("/Users/cronus/Desk")
        assert result["success"] is False
        api.assert_not_called()

    def test_partial_segment_rejected_windows(self):
        result, api = _call("C:/Users/TU", cwd="C:/Users/TUF/Desktop/demo")
        assert result["success"] is False
        api.assert_not_called()

    def test_home_ancestor_rejected(self, monkeypatch):
        """/Users（家目录的严格祖先）—— 旧实现放行，等于一个项目吞掉整台机器。"""
        monkeypatch.setenv("HOME", "/Users/cronus")
        result, api = _call("/Users")
        assert result["success"] is False
        api.assert_not_called()

    def test_filesystem_root_rejected(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/cronus")
        result, api = _call("/")
        assert result["success"] is False
        api.assert_not_called()

    def test_exact_match_accepted(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/cronus")
        result, _ = _call(CWD)
        assert result.get("success") is not False

    def test_true_parent_of_cwd_accepted(self, monkeypatch):
        """真祖先目录（按分隔符对齐、且不在家目录之上）仍放行——
        在仓库子目录里启动会话是正常场景。"""
        monkeypatch.setenv("HOME", "/Users/cronus")
        result, _ = _call("/Users/cronus/Desktop", cwd=CWD)
        assert result.get("success") is not False

    def test_case_insensitive_match_still_accepted(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/cronus")
        result, _ = _call("/users/CRONUS/desktop", cwd=CWD)
        assert result.get("success") is not False

    def test_empty_root_path_uses_cwd(self, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/cronus")
        tool = _project_create()
        with patch.object(project_tools, "_api_call", return_value={"success": True}) as api, \
             patch("os.getcwd", return_value=CWD):
            tool("demo", "", "")
        assert api.call_args[0][2]["root_path"] == CWD
