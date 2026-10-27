---
name: os-doctor
description: 诊断 AI Team OS 系统健康状态 — 运行时、待处理提示、安装面
---

# /os-doctor — 系统诊断

按顺序做下面几步，把结果和修复动作讲清楚。

1. **运行时**：调 MCP 工具 `os_health_check`。它返回 API 可达性、团队数，以及一行 token
   归因覆盖率。
   - hook 事件近 24 小时的两本账：`hook_delivery` 是 hook 侧投递失败（API 挂了也在），
     `hook_ingest` 是 API 侧入库结局（跨重启累计）。`body_lost` 大于 0 就是丢了事件；
     `client_gone` 只丢回执、是下界；`complete=false` 表示数字偏小：有进程没来得及落账
     （被强杀），或另有一个 API 实例在跑（本就不该出现，见下一条）。
   - 不健康：调 `os_restart_api`。连 MCP 工具都不可用时，请用户重启 Claude Code（服务随
     MCP 自动拉起）。**不要手动再起一个 uvicorn 实例**：它会和自动拉起的那份并存，共用
     同一个库、重复唤醒。
2. **路径**：在会话里运行 `aiteam paths --api`（等价 `python3 -m aiteam.paths --api`，只读），按输出核对：
   - `data_dir.mode` 为 `canonical`、`legacy` 或 `env` 都正常（`legacy` 表示数据还在旧位置原地
     沿用，下次更新时会尝试迁移）；`missing`、`conflict` 照对应的提示处理，**不要**新建目录或库；
   - `api.data_dir` 与 `data_dir.path` 不一致，就是本会话与正在运行的服务用的不是同一个数据目录；
   - `claude.config_dir` 是本会话的 Claude Code 配置目录（`CLAUDE_CONFIG_DIR`，未设时为默认目录），
     hook 注册面（`claude.settings`）、主链副本（`claude.main_chain_dir`）与主链归属
     （`claude.chain_owner`）都以它为准；`installs` 列出各配置目录最近一次安装的版本。
3. **全量提示清单**：调 `notice_list(status="all", limit=100)`（会先重跑一遍全部检测）。
   逐条列出：编号（design_number）、状态、用户看到的那一行、上次在终端显示的
   时间与出口（`last_shown`，空表示还没显示过）。
   - 活动（active）或到期的暂缓（snoozed）条目，给出修复动作：以该条的动作句为准，要细节
     就 `notice_list(key="<key>")` 取详情，照详情里的说明做。说明已经按用户的安装方式（插件、
     源码、Codex 适配器）给出了对应命令，不要换成别的安装方式的命令。
   - 要写用户配置或用户目录的修复（例如同步落后的装机副本），一律走 `os_config_change`：先
     预览，把预览原样给用户看，用户同意后再带 token 与用户原话应用。
   - 已清除（cleared）、已忽略（dismissed）的条目只列不修。
4. **hook 注册面与装配面**：只在当前目录是 AI Team OS 仓库 checkout（同时有 `install.py`
   与 `scripts/check_hook_surface.py`）时才做：
   - `python3 scripts/check_hook_surface.py`：校验 install.py、hooks.json 与双语 README 三方一致；
   - `python3 scripts/check_codex_installed_hooks.py`：Codex 已装副本与仓内适配器逐字节对账，
     漂移或仅装侧即退出码 1（严格版；总机检 I28 只警告，用户 2026-10-16 裁定）。退出码 1 时
     把对账表给用户看，修复走 `os_config_change("update_codex_adapter")` 先预览、经用户同意再
     应用；输出 `[WARN]` 表示本机没装 Codex 适配器，照实说明，不算故障；
   - `bash scripts/preflight.sh --fast`。
   不在仓库里（插件安装的用户）就跳过这一步，也不要让用户去跑这些脚本。

## 别把正常状态判成故障

- 库、端口文件与日志都在 OS 数据目录（`aiteam paths` 的 `data_dir`；`AITEAM_DATA_DIR` 可覆盖，
  `AITEAM_DB_PATH` 可单独指定库）。cwd 下的 `aiteam.db` 与 OS 无关——照 cwd 去找会在一台健康的
  机器上报 FAIL，而任何"修复"动作都是在制造第二个库。运行期找不到数据目录时 OS 会提示，
  不会也不要新建一个空的。
- API 端口不固定，由 MCP autostart 写在数据目录的 `api_port.txt`，不要假设 8000。
- hook 的注册面是本会话配置目录里的 `settings.json`（插件安装时另有插件自带的一份，配置目录
  那份存在时插件那份自动让位）。项目级 `.claude/settings.local.json` 里出现我方 hook 是会导致
  事件双发的残留，按 `/os-hooks` 处理，**不要往那里补写**。
- 分发版用的是 `plugin/dashboard-dist`，没有 `dashboard/node_modules` 不算缺依赖。
- 提示条目的状态是 cleared 表示问题已消失，不是故障；同一问题再出现会重新变成 active。
