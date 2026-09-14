---
name: os-doctor
description: 诊断 AI Team OS 系统健康状态 — 运行时、hook 注册面、装配面
---

# /os-doctor — 系统诊断

按顺序做这三件事，把结果和修复建议讲清楚：

1. **运行时**：调 MCP 工具 `os_health_check`。它返回 API 可达性、团队数，以及一行 token
   归因覆盖率——比任何手写的逐项检查都准。
2. **hook 注册面**（在仓库 checkout 里时）：`python3 scripts/check_hook_surface.py`，校验
   install.py ↔ hooks.json ↔ 双语 README 三方一致。这一项只有人主动跑才会发现漂移。
3. **装配面**（同上，仅在仓库里）：`bash scripts/preflight.sh --fast`。

## 别把正常状态判成故障

- 库在 `~/.claude/data/ai-team-os/aiteam.db`（`AITEAM_DB_PATH` 可覆盖）。cwd 下的
  `aiteam.db` 只是会被自动迁移的旧库，不在也没关系——照 cwd 去找会在一台健康的机器上报
  FAIL，而任何"修复"动作都是在制造第二个库。
- API 端口不固定，由 MCP autostart 写在 `~/.claude/data/ai-team-os/api_port.txt`，不要假设 8000。
- hook 的**唯一**注册面是全局 `~/.claude/settings.json`。项目级 `.claude/settings.local.json`
  里出现我方 hook 是会导致事件双发的残留，按 `/os-hooks` 处理，**不要往那里补写**。
- 分发版用的是 `plugin/dashboard-dist`，没有 `dashboard/node_modules` 不算缺依赖。
