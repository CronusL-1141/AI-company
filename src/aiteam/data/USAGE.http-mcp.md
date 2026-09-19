# Codex 共享 HTTP MCP（开发候选）

本说明对应开发分支，不表示当前 Desktop 已切换，也不修改 Claude 的 stdio 配置。

## 解决什么

Desktop 可以在任务完成后继续持有 MCP 连接。stdio 连接未关闭时，每个任务的完整 Python MCP 仍会常驻；EOF 退出修补只能处理连接真正关闭的情况，不能替宿主释放连接。

可选 HTTP 接入复用 OS API 的 `/mcp/`，不为每个任务启动完整 MCP 子进程。一次性 `http_headers_helper` 只读取自己的工作目录、检查本机 API 的明确能力标记、输出连接头后退出；不会读取账号凭据、启动模型、重启服务或保留后台进程。

## 配置与应用顺序

1. 先按本地部署流程更新并启动 OS API，确认 `GET /api/mcp/http-readiness` 返回 `project_context=connection-cwd-v1`。仅版本号相同或 health 成功不代表具有此能力；旧 API 和未启动 API 会被 helper 拒绝。
2. 仅修改 Codex 自己的 MCP 配置，将原 ai-team-os 的 `command`、`args`、stdio `env` 替换为 HTTP 配置；保存原段作为回滚依据。示例中的解释器、源码路径和端口都须替换为实际部署位置：

```toml
[mcp_servers.ai-team-os]
url = "http://127.0.0.1:8000/mcp/"
http_headers_helper = "python3 '/path/to/AI team OS/src/aiteam/mcp/http_headers.py' --api-url http://127.0.0.1:8000"
startup_timeout_sec = 30
tool_timeout_sec = 120
```

3. 让 Desktop 重新加载连接。修改文件本身不证明当前已加载；若宿主没有可用的热刷新入口，需要在其它任务可中断时完整退出再打开 Desktop。不能按进程年龄批量杀旧连接，也不能把归档任务当作连接已释放。
4. 验证工具表、两个不同项目的 `context_resolve`、取消一条等待后另一条连接仍可用，以及新任务不再产生常驻 `aiteam.mcp.server`。独立测试 app-server 成功与当前 Desktop 已应用是两种证据。

不再依靠 stdio 自动拉起 API：OS 服务未运行时，应先通过既有 OS 启动流程启动，helper 会清楚报错，不擅自替换或重启共享服务。

## 归属与隔离边界

- 使用宿主为每个 HTTP 连接执行 helper 时提供的工作目录，按已登记项目的最长路径匹配。每次工具调用使用请求局部上下文，不复用共享 API 创建者的 cwd、项目缓存或 Claude 会话环境。
- 缺失工作目录、路径不属于已登记项目或归属冲突时明确拒绝，不猜项目。
- 当前 helper 没有已验证的原生 Codex 会话 ID 来源。自动会话归属保持未知；不能把 API 创建者或父任务的环境变量当成当前调用者。需要的任务、项目等参数仍应显式填写。此方案不宣称完成所有会话身份适配。
- Claude stdio 原行为保留；本配置不改变 Claude 的技能、Hook、配置或授信。

## 回滚

恢复此前保存的 Codex stdio 配置段，再由宿主重新加载连接。不要删除共享数据库、其它任务的工作树或进程。HTTP 能力存在于 API 不要求所有客户端都使用它；两种传输可并存。
