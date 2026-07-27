---
name: os-hooks
description: 查看 AI Team OS 的 Claude Code Hooks 注册状态与修复方式
---

# /os-hooks — Hook 注册面查看

你需要帮助用户查看 AI Team OS 的 Claude Code Hooks 注册状态。

## 只有一个注册面

AI Team OS 的 hook **全部注册在全局 `~/.claude/settings.json`**，由仓库根的
`install.py` 写入，事件面以 `plugin/hooks/hooks.json` 为准（11 事件 17 条）。
脚本运行期副本在 `~/.claude/hooks/ai-team-os/`。

历史上曾有第二套「项目级」注册器（已退役的 `aiteam hooks` 子命令 → 项目
`.claude/settings.local.json`，只装 `send_event.py`、7 事件旧 matcher），
已于 2026-07-27 退役——它与全局链并存会让每个事件双发。
**不要再向项目级 settings.local.json 写 AI Team OS 的 hook。**

## 用法

- `/os-hooks` — 查看当前注册状态
- `/os-hooks repair` — 重装/修复注册面

## 操作流程

### 无参数：查看状态

1. 读取 `~/.claude/settings.json` 的 `hooks` 段，列出事件与 matcher。
2. 跑一次三方一致性机检（注册面是否与仓库定义对得上）：
   ```bash
   python3 scripts/check_hook_surface.py
   ```
3. 确认运行期脚本目录存在且非空：`~/.claude/hooks/ai-team-os/`。
4. 如果用户项目里存在 `.claude/settings.local.json` 且其 `hooks` 段含
   `send_event.py`，那是已退役的老注册器留下的残留——会造成事件双发。
   先把内容展示给用户，征得同意后再删掉该 `hooks` 段。

### repair 模式

1. 说明将要改动 `~/.claude/settings.json`（先备份）。
2. 执行：
   ```bash
   python3 install.py --update
   ```
   该命令按脚本名白名单清理我方旧条目后按 `HOOK_SURFACE` 重建，
   外来 hook（用户自己的守卫脚本等）会被原样保留。
3. 复跑 `python3 scripts/check_hook_surface.py` 确认全绿。

## 输出格式

### 状态查看
```
## Hooks 注册状态

注册面: ~/.claude/settings.json（全局，唯一）
已注册 11 个事件 / 17 条：
- SessionStart / SubagentStart / SubagentStop / PreToolUse / PostToolUse
- SessionEnd / Stop / TaskCreated / PermissionDenied / UserPromptSubmit / PreCompact

运行期脚本: ~/.claude/hooks/ai-team-os/（10 个）
API 目标: http://localhost:<api_port.txt>/api/hooks/event
三方一致性机检: ✅ install.py ↔ hooks.json ↔ README
```

## 注意

- 所有输出使用中文
- 改动 `~/.claude/settings.json` 前必须备份，并保留外来 hook 条目
- 确保 API 服务已启动：`/os-up`
