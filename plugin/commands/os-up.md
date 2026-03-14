---
name: os-up
description: 一键启动AI Team OS服务 — API + Dashboard
---

# /os-up — 一键启动服务

启动 AI Team OS 的 API 服务器和 Dashboard 前端。

## 操作步骤

### 1. 检测API服务
```bash
curl -s --max-time 2 http://localhost:8000/api/teams
```
- 如果返回JSON → API已在运行，跳到步骤3
- 如果超时/失败 → 执行步骤2

### 2. 启动API服务（后台）
```bash
cd C:/Users/TUF/Desktop/AI团队框架/ai-team-os
python -m uvicorn aiteam.api.app:create_app --host 0.0.0.0 --port 8000 --factory &
```
等待3秒后验证：
```bash
curl -s --max-time 3 http://localhost:8000/api/teams
```

### 3. 检测Dashboard
```bash
curl -s --max-time 2 http://localhost:5173
```
- 如果返回HTML → Dashboard已在运行，跳到步骤5
- 如果超时/失败 → 执行步骤4

### 4. 启动Dashboard（后台）
```bash
cd C:/Users/TUF/Desktop/AI团队框架/ai-team-os/dashboard
npm run dev &
```
等待5秒后验证：
```bash
curl -s --max-time 3 http://localhost:5173
```

### 5. 报告状态

显示：
```
AI Team OS 服务状态:
- API: http://localhost:8000 ✅/❌
- Dashboard: http://localhost:5173 ✅/❌
- API文档: http://localhost:8000/docs
- WebSocket: ws://localhost:8000/ws
```

## 注意
- 使用后台方式启动（`&`），不阻塞当前会话
- 启动前检测避免重复启动
- Dashboard使用npm run dev（开发模式），关闭时用Ctrl+C或'q'
- 如果启动失败，提示检查依赖：`pip install -e ".[all]"` 和 `cd dashboard && npm install`
