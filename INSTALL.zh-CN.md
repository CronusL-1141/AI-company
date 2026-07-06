# AI Team OS 安装指南（供 Claude Code 阅读）

本文件供 Claude Code (CC) 辅助安装时阅读。如果你是人类用户，请查看 [README.zh-CN.md](README.zh-CN.md)。

共有**两条受支持的安装路线**：

- **路线 A — 插件 marketplace**（推荐给大多数用户）：无需 clone，自动配置。
- **路线 B — 从源码安装**（推荐给开发者 / 贡献者）：editable 安装，始终跟随最新源码。

> 原「pip install ai-team-os」（PyPI）路径已**删除**：wheel 不包含 `plugin/` 与配置资源，功能不全，且其安装后置命令从来无效。请改用路线 A 或路线 B。

---

## 安装前检查

安装前请验证 Python 环境：

1. 运行：`python3 -c "import sys; print(sys.executable); print(sys.prefix); print(sys.base_prefix)"`
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
python3 -c "
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

### 路线 A：插件 marketplace（推荐给大多数用户）

```bash
# 添加 marketplace 源并安装插件
claude plugin marketplace add CronusL-1141/AI-company
claude plugin install ai-team-os

# 重启一次 Claude Code。
```

**安装后首次启动**时，`auto_install` hook 会自动运行并：
- 从源码（GitHub）把 Python 依赖装进你的系统 Python；
- 把插件清单的解释器自愈为 `sys.executable`（绝对路径），使得即便在 stock macOS（无 `python` shim）或项目 `.venv` 激活时，MCP + 全部 hook 仍能正常工作；
- 打印 "Please restart Claude Code to activate all features."

因此完整顺序是：安装 → 重启 →（首次启动自动装依赖，约 30 秒，仅一次）→ 再重启一次。之后一切就绪。

### 路线 B：从源码安装（推荐给开发者 / 贡献者）

```bash
# 克隆仓库
git clone https://github.com/CronusL-1141/AI-company.git
cd AI-company

# 运行安装程序（针对你的工作区配置 MCP + Hooks + Agent 模板）
python3 install.py

# 重启 Claude Code
```

这会把包**以 editable 方式装到你的工作区**，因此始终跟随你 pull 的最新源码。`greenlet` 现已是核心依赖，Apple Silicon（arm64）macOS 可直接安装、无需额外步骤。LangGraph 遗留 CLI 路径现降为可选 extra，仅当你需要 `aiteam task run` 时才安装：

```bash
pip install 'ai-team-os[langgraph]'
```

### Homebrew / PEP 668（externally-managed-environment）

在 Homebrew Python 或任意 PEP 668 环境下，`pip` 可能拒绝装入系统 Python 并报 `externally-managed-environment` 错误。AI Team OS 的**设计**就是要住在系统 Python 里（全局 hook 脚本依赖它——见下方 venv 警告），因此请设置：

```bash
export PIP_BREAK_SYSTEM_PACKAGES=1
```

再运行安装（路线 B）或首次启动的自动安装（路线 A）。**不要**通过装入 venv 来绕过此错误。

---

## 验证安装

重启 Claude Code 后：

1. 运行 `/mcp` — `ai-team-os` 应显示为已连接，约 155 个工具
2. 运行 `os_health_check` MCP 工具 — 预期响应：`{"status": "ok"}`
3. 检查 API：`curl http://localhost:8000/api/health` — 预期：`{"status": "ok"}`

如果工具未显示，检查 `~/.claude.json` 中的**全局 MCP 注册**（CC 从这里读取全局 MCP 服务器，**不是** `settings.json`）：
- 在 `~/.claude.json` 的 `mcpServers` 下查找 `ai-team-os`
- Windows 上该文件为 `%USERPROFILE%\.claude.json`

---

## 已知限制

- **不要在项目 `.venv` 中安装** — 全局 hook 脚本依赖系统 Python。在 venv 中安装意味着 AI Team OS 仅在该 venv 激活时可用。
- 如果误装在 venv 中：`pip uninstall ai-team-os`，然后 `deactivate`，然后重装到系统 Python。
- 需要 Python >= 3.11。
- 需要支持 MCP 的 Claude Code（CC 版本 >= 1.0）。

---

## 更新

```bash
# 路线 A（插件）：
claude plugin update ai-team-os@ai-team-os

# 路线 B（源码）：
git pull
python3 install.py --update
```

## 卸载

```bash
# 路线 A（插件）：
claude plugin uninstall ai-team-os

# 路线 B（源码）：
python3 scripts/uninstall.py

# 清理残留数据：
# Windows: rmdir /s %USERPROFILE%\.claude\plugins\data\ai-team-os-ai-team-os
# macOS/Linux: rm -rf ~/.claude/plugins/data/ai-team-os-*
```
