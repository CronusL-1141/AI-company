# AI Team OS 安装指南（供 Claude Code 阅读）

本文件供 Claude Code (CC) 辅助安装时阅读。如果你是人类用户，请查看 [README.zh-CN.md](README.zh-CN.md)。

---

## 安装前检查

安装前请验证 Python 环境：

1. 运行：`python -c "import sys; print(sys.executable); print(sys.prefix); print(sys.base_prefix)"`
2. 如果输出中 `sys.prefix` 包含 `.venv`、`virtualenv` 或 `env` 路径，**警告用户**：

   > ⚠️ 警告：你当前处于虚拟环境中（`{sys.prefix}`）。
   > AI Team OS 应安装到系统 Python，而非项目 venv。
   > 如果安装在 venv 中，hook 脚本在其他项目中将无法工作。
   > 请先执行 `deactivate`，或打开一个没有 venv 的新终端，然后重新安装。

3. 确认 `sys.prefix == sys.base_prefix` — 表示当前在系统 Python 中，可以安全安装。

---

## 必需：配置设置

AI Team OS 需要在 `~/.claude/settings.json` 中配置特定设置。**运行以下脚本自动完成配置**：

```bash
python -c "
import json, os

p = os.path.join(os.path.expanduser('~'), '.claude', 'settings.json')
os.makedirs(os.path.dirname(p), exist_ok=True)

settings = {}
if os.path.exists(p):
    with open(p) as f:
        settings = json.load(f)

changed = False

# 1. 启用 Agent Teams（团队协作必需）
env = settings.setdefault('env', {})
if env.get('CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS') != '1':
    env['CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS'] = '1'
    changed = True
    print('[OK] Agent Teams 已启用')

# 2. effortLevel — 由用户自行选择，不自动设置

if changed:
    with open(p, 'w') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    print('[完成] 设置已更新 — 请告知用户重启 Claude Code')
else:
    print('[OK] 所有设置已就绪')
"
```

**运行后，告知用户重启 Claude Code** 以使设置生效。

---

## 安装步骤

### 方式 A：插件安装（推荐）

```bash
# 添加 marketplace 源并安装插件
claude plugin marketplace add CronusL-1141/AI-company
claude plugin install ai-team-os

# 重启 Claude Code
# 首次启动约需30秒配置依赖（仅一次）
```

### 方式 B：从源码安装

```bash
# 克隆仓库
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company

# 运行安装程序（配置 MCP + Hooks + Agent 模板）
python install.py

# 重启 Claude Code
```

### 方式 C：pip 安装（PyPI）

```bash
# 从 PyPI 安装
pip install ai-team-os

# 运行安装后配置脚本（必需 — 设置 MCP + hooks 配置）
python -m aiteam.cli.app init

# 重启 Claude Code
```

---

## 验证安装

重启 Claude Code 后：

1. 运行 `/mcp` — `ai-team-os` 应显示为已连接，约 107 个工具
2. 运行 `os_health_check` MCP 工具 — 预期响应：`{"status": "ok"}`
3. 检查 API：`curl http://localhost:8000/api/health` — 预期：`{"status": "ok"}`

如果工具未显示，检查：
- Windows：`%USERPROFILE%\.claude\settings.json` — 查找 `mcpServers` 中的 `ai-team-os`
- macOS/Linux：`~/.claude/settings.json`

---

## 已知限制

- **不要在项目 `.venv` 中安装** — 全局 hook 脚本依赖系统 Python。在 venv 中安装意味着 AI Team OS 仅在该 venv 激活时可用。
- 如果误装在 venv 中：`pip uninstall ai-team-os`，然后 `deactivate`，然后重装。
- 需要 Python >= 3.11。
- 需要支持 MCP 的 Claude Code（CC 版本 >= 1.0）。

---

## 更新

```bash
# 插件安装：
claude plugin update ai-team-os@ai-team-os

# 手动/pip 安装：
pip install --upgrade ai-team-os
```

## 卸载

```bash
# 插件安装：
claude plugin uninstall ai-team-os

# 手动安装：
python scripts/uninstall.py

# 清理残留数据：
# Windows: rmdir /s %USERPROFILE%\.claude\plugins\data\ai-team-os-ai-team-os
# macOS/Linux: rm -rf ~/.claude/plugins/data/ai-team-os-*
```
