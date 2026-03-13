---
name: os-up
description: 启动AI Team OS服务 — FastAPI服务器和Dashboard
---

# /os-up — 启动服务

你需要帮助用户启动 AI Team OS 的后端 API 服务。

## 操作步骤

1. 检查是否已有服务在运行（尝试 GET http://localhost:8000/api/teams ，timeout 2秒）
2. 如果已在运行，告知用户服务已就绪，显示访问地址
3. 如果未运行，在后台启动 FastAPI 服务器：
   ```bash
   python -m uvicorn aiteam.api.app:create_app --host 0.0.0.0 --port 8000 --factory &
   ```
4. 等待2秒后再次检测服务是否成功启动
5. 展示服务状态

## 输出格式

### 服务已在运行
```
## AI Team OS 服务状态

服务已在运行中。

- API: http://localhost:8000
- API文档: http://localhost:8000/docs
- Dashboard: http://localhost:3000

使用 `/os-status` 查看系统详情。
```

### 启动成功
```
## AI Team OS 服务启动成功

- API: http://localhost:8000
- API文档: http://localhost:8000/docs
- WebSocket: ws://localhost:8000/ws

### 后续步骤
- `/os-status` 查看系统状态
- `/os-init` 初始化项目（如果尚未初始化）
- `/os-meeting create <主题>` 创建会议
```

### 启动失败
```
## 服务启动失败

请检查:
1. Python 环境是否已安装依赖: `pip install -e ".[all]"`
2. 端口 8000 是否被占用
3. 查看错误日志排查问题
```

## 注意

- 所有输出使用中文
- 使用后台方式启动，不阻塞当前会话
- 不要使用 `--reload` 参数（生产模式）
- 启动前检测避免重复启动
