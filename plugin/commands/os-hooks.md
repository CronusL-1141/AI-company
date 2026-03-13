---
name: os-hooks
description: 管理Claude Code Hooks配置 — 安装、移除、查看状态
---

# /os-hooks — Hook 配置管理

你需要帮助用户管理 AI Team OS 的 Claude Code Hooks 集成。

## 用法

- `/os-hooks` — 显示当前 hooks 配置状态
- `/os-hooks install` — 安装 hooks 到当前项目
- `/os-hooks remove` — 移除 hooks 配置

## 操作流程

### 无参数：查看状态

1. 检查 `.claude/settings.local.json` 是否存在
2. 读取其中的 hooks 配置
3. 展示已配置的事件列表和状态

### install 模式

1. 执行安装命令：
   ```bash
   python -m aiteam.cli.app hooks install .
   ```
2. 验证安装结果：检查 `.claude/settings.local.json` 已更新
3. 展示已安装的 hook 事件列表

### remove 模式

1. 确认用户意图（移除后 CC 操作不会再同步到 Dashboard）
2. 执行移除命令：
   ```bash
   python -m aiteam.cli.app hooks remove .
   ```
3. 确认移除成功

## 输出格式

### 状态查看
```
## Hooks 配置状态

已配置 7 个 hook 事件：
- SubagentStart — Agent启动时触发
- SubagentStop — Agent停止时触发
- PreToolUse — 工具调用前触发 (matcher: Agent|Bash|Edit|Write)
- PostToolUse — 工具调用后触发 (matcher: Agent|Bash|Edit|Write)
- SessionStart — 会话开始时触发
- SessionEnd — 会话结束时触发
- Stop — 停止时触发

API 目标: http://localhost:8000/api/hooks/event
脚本位置: .claude/hooks/send_event.py
```

### 安装成功
```
## Hooks 安装完成

已安装 7 个 hook 事件到 .claude/settings.local.json
CC 中的操作现在会自动同步到 AI Team OS Dashboard。

确保 API 服务已启动: `/os-up`
```

## 注意

- 所有输出使用中文
- 安装前检查是否已有配置，避免重复安装
- 移除前必须征得用户同意
