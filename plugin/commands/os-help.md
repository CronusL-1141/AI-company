---
name: os-help
description: 显示AI Team OS所有可用命令和使用帮助
---

# /os-help — 帮助信息

直接向用户展示 AI Team OS 的所有可用斜杠命令和使用说明。

## 输出内容

展示以下帮助信息：

```
## AI Team OS — 命令帮助

### 核心命令
| 命令 | 说明 |
|------|------|
| `/os-status` | 显示系统状态 — 团队、Agent、会议概览 |
| `/os-up` | 启动 API 服务器 |
| `/os-init` | 初始化项目 — 生成配置文件 |
| `/os-doctor` | 诊断系统健康状态 |

### 协作命令
| 命令 | 说明 |
|------|------|
| `/os-meeting` | 会议管理 — 创建、查看会议 |
| `/os-meeting create <主题>` | 快速创建会议 |
| `/os-task` | 任务管理 — 查看、执行任务 |
| `/os-task run <描述>` | 创建并执行任务 |

### 配置命令
| 命令 | 说明 |
|------|------|
| `/os-hooks` | 查看 Hooks 配置状态 |
| `/os-hooks install` | 安装 CC Hooks |
| `/os-help` | 显示本帮助信息 |

### Skills (团队成员可用)
| Skill | 说明 |
|-------|------|
| `/os-register` | 向 OS 注册当前 Agent |
| `/meeting-participate` | 作为参与者加入会议 |
| `/meeting-facilitate` | 作为主持人管理会议 |

### 快速上手
1. `/os-init` — 初始化项目配置
2. `/os-up` — 启动服务
3. `/os-status` — 确认系统就绪
4. `/os-meeting create 架构讨论` — 开始第一次团队会议

### 更多信息
- API 文档: http://localhost:8000/docs
- Dashboard: http://localhost:3000
- 项目文档: docs/architecture.md
```

## 注意

- 所有输出使用中文
- 直接输出帮助信息，无需调用任何 API
- 保持格式简洁清晰
