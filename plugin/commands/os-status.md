---
name: os-status
description: 显示AI Team OS系统状态 — 团队、Agent、会议概览
---

# /os-status — 系统状态概览

你需要查询 AI Team OS API 并向用户展示系统当前的运行状态。

## 操作步骤

1. 调用 MCP tool `team_list` 获取所有团队列表
2. 对每个团队，调用 `agent_list` 获取其 Agent 列表
3. 对每个团队，调用 `meeting_list` 获取活跃会议（status=active）
4. 将结果整理为清晰的状态报告

## 输出格式

按以下结构组织输出：

```
## AI Team OS 系统状态

### 团队
- 团队名称 | 编排模式 | Agent数量

### Agent一览
- Agent名称 | 角色 | 状态(IDLE/BUSY/OFFLINE) | 所属团队

### 活跃会议
- 会议主题 | 参与者 | 消息数 | 所属团队

### 系统信息
- API地址: http://localhost:8000
- Dashboard: http://localhost:3000
```

## 错误处理

如果 API 不可达，输出：
> AI Team OS 服务未运行。请先执行 `/os-up` 启动服务。

## 注意

- 所有输出使用中文
- Agent 状态用不同标记区分: IDLE=空闲, BUSY=忙碌, OFFLINE=离线
- 如果没有任何团队，提示用户使用 `/os-init` 初始化项目
