---
name: os-register
description: Auto-register as a team member when joining an AI Team OS project
autoTrigger: true
---

# OS Register — 启动注册与状态汇报

当你作为团队成员启动时，必须立即向 AI Team OS 注册自己。这确保 OS 能追踪你的存在、状态和活动。

## 步骤

### 0. 读取系统规则

启动后第一件事——读取并遵守OS系统规则：

```
使用 Bash: curl -s http://localhost:8000/api/system/rules | python -c "import json,sys; rules=json.load(sys.stdin); [print(f'  [{r[\"id\"]}] {r[\"name\"]}') for r in rules.get('advisory_rules',[])]"
```

这些规则指导你的行为（团队管理、会议组织、任务分配等），必须遵守。

规则已通过SessionStart自动注入，可通过 `GET /api/system/rules` 查看完整规则。

> 正常情况下Agent注册已由hook_translator自动完成，此skill为手动备份流程。

### 1. 健康检查

首先确认 OS API 服务可达：

```
使用 MCP tool: os_health_check
```

如果返回 `unhealthy`，尝试自动启动服务：

```bash
cd C:/Users/TUF/Desktop/AI团队框架/ai-team-os
python -m uvicorn aiteam.api.app:create_app --host 0.0.0.0 --port 8000 --factory &
```

等待3秒后重试 `os_health_check`。如果仍然失败，跳过注册，不影响你的正常工作。

### 2. 确定团队

检查是否有目标团队：

```
使用 MCP tool: team_list
```

- 如果你知道要加入的团队名称，从列表中找到对应的 `team_id`
- 如果团队不存在，使用 `team_create` 创建
- 如果未指定团队，加入列表中的第一个团队

### 3. 注册自己

向团队注册：

```
使用 MCP tool: agent_register
参数:
  team_id: <目标团队ID>
  name: <你的名称>
  role: <你的角色描述>
  model: <你使用的模型，如 claude-opus-4-6>
  system_prompt: <你的职责描述>
```

**重要**: 记录返回的 `agent_id`，后续所有操作都需要用到。

### 4. 更新状态为 BUSY

注册完成后立即标记自己为工作中：

```
使用 MCP tool: agent_update_status
参数:
  agent_id: <你的agent_id>
  status: "busy"
```

### 5. 阅读注册返回的团队快照

`agent_register` 返回值已包含 `team_snapshot`（队友列表、待办任务详情、最近会议），直接阅读：

- **如果 `pending_tasks` 中有分配给你的任务** → 立即开始执行，无需等待Leader指令
- **如果有未分配的待办任务** → 向Leader请示是否由你接手
- **如果没有待办任务** → 告知Leader你已就绪，等待分配
- **查看 `teammates` 列表** → 了解队友是谁、在做什么，避免重复工作
- **查看 `recent_meeting`** → 了解最近的讨论和决策

> 注意：无需额外调用 `team_briefing`，注册返回值已包含所需信息。仅在需要查看最近事件详情时才调用 `team_briefing`。

### 6. 完成任务后更新状态

当你完成所有工作准备退出时，将状态设为 idle：

```
使用 MCP tool: agent_update_status
参数:
  agent_id: <你的agent_id>
  status: "idle"
```

## 注意事项

- 注册是幂等的：如果你已经注册过（同名同团队），API 会返回已有的 agent 记录
- 始终在开始工作前完成注册，这是参与团队协作的前提
- 你的 `agent_id` 在会议发言、任务分配等场景中都会用到，务必保存
- 注册后 `agent_register` 返回值已包含完整 `team_snapshot`（队友列表、待办任务详情、最近会议），无需额外调用 `team_briefing`
- 新增MCP tool后执行 /mcp → 选择 ai-team-os → Reconnect 刷新工具列表，无需重启CC
