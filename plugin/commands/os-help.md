---
name: os-help
description: 介绍 AI Team OS 能做什么，并按实际安装情况列出可用的命令与技能
---

# /os-help — 帮助信息

**不要背诵写死的命令表**。命令与技能会增减，上一版硬编码的清单同时烂在三处（漏了三个
技能、写错子命令、写错端口）。现读现答：

- 可用命令与技能：本次会话已加载的技能描述列表，加上 `~/.claude/commands/` 与插件
  `commands/` 目录里的文件名。
- 工具能力：ai-team-os 这个 MCP server 的 server instructions 已按能力分组列在你的上下文里。
- 三个常用入口：任务墙 `task_list_project`、记忆 `memory_search`、报告 `report_list`。

## 讲清楚这几件事

- OS 是**跨会话的持久化治理层**：CC 会话是临时的，任务、记忆、报告、观测记录跨会话长存。
- **没有初始化这一步**：团队由 SubagentStart hook 在第一次派工时自动建。
- Dashboard 与 API **同端口**，端口在 `~/.claude/data/ai-team-os/api_port.txt`。
- 发版走技能 `/os-release`；跨 harness 给另一个 AI 留言走 `/os-channel`；开会走
  `/os-meeting` 与技能 meeting-facilitate。
