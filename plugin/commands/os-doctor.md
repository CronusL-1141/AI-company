---
name: os-doctor
description: 诊断AI Team OS系统健康状态 — 检查服务、配置、依赖
---

# /os-doctor — 系统诊断

你需要对 AI Team OS 进行全面的健康检查，帮助用户排查问题。

## 检查项目

按顺序执行以下检查，每项标记为通过/失败/警告：

### 1. Python 环境
- 检查 Python 版本 >= 3.12
- 检查 `aiteam` 包是否已安装: `python -c "import aiteam; print(aiteam.__version__)"`

### 2. 配置文件
- 检查 `aiteam.yaml` 是否存在
- 检查 `.aiteam/` 目录是否存在

### 3. API 服务
- 检查 http://localhost:8000/api/teams 是否可达（timeout 2秒）
- 如果可达，显示响应状态

### 4. 数据库
- 检查 `aiteam.db` 文件是否存在（SQLite模式）
- 或检查 PostgreSQL 连接（如果配置了）

### 5. Hooks 配置
- 检查 `.claude/settings.local.json` 中是否配置了 hooks
- 检查 hook 脚本文件是否存在

### 6. Dashboard
- 检查 `dashboard/` 目录是否存在
- 检查 `dashboard/node_modules/` 是否已安装依赖

## 输出格式

```
## AI Team OS 系统诊断

| 检查项 | 状态 | 详情 |
|--------|------|------|
| Python 环境 | PASS | Python 3.12.x |
| aiteam 包 | PASS | v0.3.0 |
| 配置文件 | PASS | aiteam.yaml 存在 |
| API 服务 | FAIL | http://localhost:8000 不可达 |
| 数据库 | PASS | aiteam.db (SQLite) |
| Hooks 配置 | WARN | 已配置 5/7 个事件 |
| Dashboard | PASS | 依赖已安装 |

### 建议
- API 服务未运行，请执行 `/os-up` 启动
- Hooks 配置不完整，请执行 `aiteam hooks install` 重新安装
```

## 注意

- 所有输出使用中文
- 状态标记: PASS=通过, FAIL=失败, WARN=警告
- 对每个失败项给出具体的修复建议
- 使用 Bash 工具执行检查命令
