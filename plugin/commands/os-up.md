---
name: os-up
description: 确认 AI Team OS 服务在跑，异常时按正确方式重启
---

# /os-up — 启动/确认服务

**API 通常不需要你启动**：MCP 的 autostart 在会话建立时已经把它拉起，并把端口写进
`~/.claude/data/ai-team-os/api_port.txt`。

1. 确认：调 MCP 工具 `os_health_check`。
2. 不健康时：调 MCP 工具 `os_restart_api`。它保留端口发现、所有权校验与版本对齐。
3. **不要手工 `uvicorn` 起一份**。手工实例绕开端口发现，会和 autostart 那份共用同一个
   SQLite 库，观测写入互相覆盖。万一确实要手工起：绑 `127.0.0.1`、用 `api_port.txt` 里的
   端口——这个 API 带写端点且无鉴权，绑 `0.0.0.0` 等于把任务/记忆/团队的增删改暴露给整个
   局域网，而且服务照样起得来、curl 照样通，你不会察觉。

## Dashboard

由 API 在**同一个端口**以 SPA 方式提供，打开 API 地址就是看板，不需要单独启动、也没有
独立端口。只有要改前端源码时才 `cd dashboard && npm run dev`（vite 端口 5174）。
