# 非默认配置目录兼容设计（CLAUDE_CONFIG_DIR / CODEX_HOME）

- 状态：维护者已裁定（2026-10-22）：数据目录取方案 B；老用户迁移改为**更新时由安装器代为完成**，过 §4.1 的安全闸才搬，不过就原地沿用；决策点 2-4 按推荐；§10 减法清单第 1-3、5-7 条批准，第 4 条第 3 批按「不剥」处理；发版等本设计做完，与 4459b1e、Ultrafast 计价、测试隔离一起发 public
- 日期：2026-10-22（美西时间）
- 基线：`4459b1e`，分支 `feat/config-dir-compat`，工作树 `.worktrees/config-dir-compat`
- 任务：#5160c89c（路径不认 CLAUDE_CONFIG_DIR）、#b7d973fd（orphan 误报）
- 范围：只出设计，不改代码；盘点数字由 §3 的脚本口径得出，可复算

## 0. 一页摘要

2026-10-22 的事故把一个一直存在的建模错误暴露出来：**OS 的数据目录（Claude 与 Codex 共用、机器级）寄居在 Claude Code 的配置目录里**（`~/.claude/data/ai-team-os`），而 Claude 的配置目录是会被外部工具整体搬走的（一个多账号切换工具把 `~/.claude` 整个搬进它自己的账号目录，再设 `CLAUDE_CONFIG_DIR` 指过去）。于是 1.65GB 的库跟着走了，hook 在原地新建空目录写运行态，API 靠旧句柄撑着；安装器把 MCP 写进 `~/.claude.json`，而会话读的是 `$CLAUDE_CONFIG_DIR/.claude.json`；SessionStart 把「数据目录不在」误判成「插件被卸载」。

设计结论（维护者已裁定，详见 §4）：

| 决策点 | 结论 |
|---|---|
| 1. 数据目录 | 换到中性目录 `~/.aiteam`（`AITEAM_DATA_DIR` 可覆盖）。老用户的库由**用户显式触发的更新动作**（`install.py --update`、`scripts/update.py`、`codex_adapter.py update`）代为迁移：先过 §4.1 的七道安全闸才搬，任一不过就退回旧位置原地沿用、发 `data_dir_legacy` 说明原因与手动命令；插件路径（`auto_install.py` 在 SessionStart 里）没有资格停共享 API，只通知、给一键命令、不强迁；**运行期永不新建、永不搬动数据目录**，找不到就大声失败（no-data≠zero 的机检形态） |
| 2. 多套 Claude 配置目录 | 安装器只装进**当前**配置目录（`CLAUDE_CONFIG_DIR`，否则 `~/.claude`；加 `--config-dir`）；hook 副本仍放在**该配置目录**的 `hooks/ai-team-os/`；settings.json 命令在 POSIX 上写成 `"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/ai-team-os/<脚本>"`，目录整体搬走也不失效 |
| 3. 插件装法 vs 源码装法 | 主链归属标记从机器级 `main-chain.json` 改为**跟随主链**的 `<配置目录>/hooks/ai-team-os/.chain-owner.json`；缺标记 = 未知 ≠ 插件；orphan 只在「标记说插件装的 + 同一配置目录里插件已移除/停用」时报；/os-doctor 与各命令改为调用 `aiteam paths` 看会话实际解析的路径，不再在 Markdown 里拼写路径 |
| 4. Codex 口径 | 与 Claude 侧共用 `hook_core.py` 的路径解析段（三份逐字节副本，I1 对钉）；`CODEX_HOME` 空串按未设置、非空按字面路径；Codex 侧只有 1 处真正写死、3 处空串读成当前目录，一并收口；两套安装只在 OS 数据目录这一处相交 |

新增机检 I30（加法，直接做）：宿主/数据目录的字面拼写只许出现在 `hook_core.py`，其余位置用棘轮计数只减不增（§8）。

## 1. 问题与证据

### 1.1 事发机器上的事实（2026-10-22 采集）

下表的「搬走后的目录」指多账号切换工具为当前账号建的配置目录（形如 `<家目录>/<工具目录>/accounts/<账号>/config`），具体位置因工具而异。

| 事实 | 证据 |
|---|---|
| 切换工具于 12:39Z 完成迁移，整个 `~/.claude` 被搬进搬走后的目录 | 工具自己的迁移记录：`config_moved` 为真，完成时间 12:39:07Z |
| 事发会话的 `CLAUDE_CONFIG_DIR` 指向搬走后的目录；`CODEX_HOME` 未设 | `env` |
| 会话实际读写的全局配置是 `$CLAUDE_CONFIG_DIR/.claude.json`（75KB，持续更新），`~/.claude.json` 只有 5KB、早已不再更新 | 两份文件的大小与修改时间 |
| 1.65GB 的真库被手工搬回 `~/.claude/data/ai-team-os/aiteam.db`；搬走后的目录里 `data/ai-team-os` 是指向它的符号链接，旁边还有一个早期留下的 0 字节 `data/aiteam.db` | 两个 data 目录的列表 |
| 真库离家期间，hook 在 `~/.claude/data/ai-team-os/` 新建了空目录并写了 notice-local、diagnostics、wake-state、mcp-debug.log；这些文件已另行归档 | 归档副本 |
| orphan 误报三次（12:53Z、13:00Z、13:02Z），均为 `SessionStart:startup`，`catalog_id=orphan_main_chain` | 归档的 `notice-local.cc.jsonl` |
| 两套 settings.json 各 24 条 hook，命令全是 `~/.claude/hooks/ai-team-os/` 绝对路径；两套 `.claude.json` 都已手工注册 MCP | 解析两份 settings.json / .claude.json |
| `~/.claude/hooks/ai-team-os/` 是当天早些时候重装的新副本（15 个文件）；搬走后的目录里的 `hooks/ai-team-os/` 是旧副本（13 个文件），**没有任何 settings.json 指向它** | 两个目录的列表 |
| 两边 `plugins/` 都没有 `installed_plugins.json`：这台机器是源码安装，没装插件 | `ls plugins/` |
| API 进程从迁移之前就一直在跑（`uvicorn aiteam.api.app:create_app --fd 4`），它打开的是搬走前的库句柄 | `ps` |

### 1.2 三条故障链

**链 A：库离家。** `storage/connection.py:71` 把数据目录写死为 `Path.home()/".claude"/"data"/"ai-team-os"`，另有 50 处同样写死（§3）。切换工具搬走 `~/.claude` 时库一起走；运行中的 API 靠旧句柄继续服务，但任何重启都会在 `~/.claude/data/ai-team-os/` 新建空库并当作正常——这正是 no-data≠zero 反对的形态。hook 侧已经发生：它们在新建的空目录里写了运行态（归档证据）。

**链 B：MCP 落错配置。** `install.py:565/586/652` 读写 `Path.home()/".claude.json"`；Claude Code 文档未记载 `.claude.json` 随 `CLAUDE_CONFIG_DIR` 移动，但事发机器上实测它就在 `$CLAUDE_CONFIG_DIR/.claude.json`（附录 A）。安装器优先走 `claude mcp add-json --scope user`，失败后兜底直写 `~/.claude.json`，安装后自检也读 `~/.claude.json`，于是「自检 OK、会话里没有 MCP」。

**链 C：orphan 误报。** `session_bootstrap.py:749 _orphan_main_chain`：读 `cc_config_dir()/settings.json`（已认 `CLAUDE_CONFIG_DIR`，看到 24 条 hook → 已注册）→ 读 `os_data_dir()/install_path.txt`（机器级写死 `~/.claude/data/ai-team-os`，当时整个目录不在 → 不存在）→ 读 `main-chain.json`（不在）→ **`marker.get("installed_by", "plugin")` 把「没有标记」默认成「插件装的」** → 配置目录里没有已启用插件 → 判 orphan，并引导跑 `uninstall_main_chain.py --apply` 拆掉正常的主链。根因是两条：数据目录位置错（链 A 的派生），以及「无证据」被默认成「插件」。

### 1.3 同一代码库里的两种口径

已经认 `CLAUDE_CONFIG_DIR` 的 10 处：`session_bootstrap.py:385`（×2 副本）、`user_notice.py:680/1485`（×3 副本）、`uninstall_main_chain.py:76`（×2）、`services/notices/install_kind.py:49`、`services/notices/transcript.py:40`、`api/language.py:32`。它们是 user-notice 那批（docs/user-notice-design.md §路径解析）按局部规则改的；其余 80 处仍写死。Codex 侧相反：16 处里 15 处读 `CODEX_HOME`，但有四种写法（§4.4）。

## 2. 路径模型：三类目录，各归其主

| 类 | 归属 | 内容 | 解析规则（目标态） |
|---|---|---|---|
| **OS 数据目录** | OS 自己，机器级，Claude/Codex 共用 | `aiteam.db`（含 WAL）、`api_port.txt`、`api.pid`、`notice-local.<host>.jsonl`、`wake-state/`、`wake_config.json`、`webhook_config.json`、`diagnostics/`、`debug.log`、`mcp-debug.log`、`api-stderr.log`、`hook-delivery/`、`dismissed_projects.json`、`install-state.json`、`install_path.txt`、`installs.json`、`compact-events.jsonl`、`arm-hint.off` | `AITEAM_DATA_DIR` → `~/.aiteam` → 旧位置 `~/.claude/data/ai-team-os`（原地沿用）→ 找不到即失败，**运行期不创建** |
| **Claude 配置目录** | Claude Code，按会话，可多套 | `settings.json`、`.claude.json`、`hooks/ai-team-os/`（OS 的主链副本）、`agents/`、`skills/`、`commands/`、`loop.md`、`plugins/`、`projects/`、`sessions/`、`teams/`、`jobs/` | `CLAUDE_CONFIG_DIR` → `~/.claude`。同一函数 `cc_config_dir()`；`.claude.json` 在该目录下 |
| **Codex home** | Codex，按安装 | `config.toml`、`hooks.json`、`hooks/ai-team-os-observer/`、`sessions/`、`archived_sessions/`、`state_5.sqlite`、`.aiteam-codex-install.json`、`ai-team-os/runtime/` | `CODEX_HOME`（非空、字面）→ hook 自身位置推断（仅 hook 侧）→ `~/.codex` |

三个原则：

1. **数据归 OS，不进任何宿主的配置目录。** 宿主配置目录里只放「指向 OS 的指针」（settings.json 的命令、`.claude.json` 的 mcpServers、config.toml 的 mcp_servers）和宿主要求放在那里的资产（agents/skills/commands/主链副本）。
2. **一个解析器，三份字节副本。** 路径解析放进 `hook_core.py`（plugin/hooks、src/aiteam/hooks、plugin/harness/codex/hooks 三处逐字节相同，I1 对钉）；包内代码经 `aiteam.paths` 薄层 re-export `aiteam.hooks.hook_core` 的函数，不另写第二份逻辑。hook 只能用标准库，解析器也只用标准库。
3. **运行期只解析，安装器才创建、才搬动。** API、MCP、hook、CLI 找不到数据目录一律失败并说明候选位置；只有 `install.py`、插件 `auto_install.py` 首跑、`scripts/codex_adapter.py install`、显式 `AITEAM_DATA_DIR`/`AITEAM_DB_PATH` 允许 mkdir；搬动旧数据只有用户显式触发的更新动作（§4.1 七道闸）。这是 no-data≠zero 在代码里的落点。

## 3. 盘点

口径：在 `4459b1e` 上扫 `src/aiteam`、`plugin`、`scripts`、`install.py` 的 `*.py`，模式为派工书的三条 grep 加一条派工书漏掉的拼写 `os.path.join(os.path.expanduser("~"), ".claude", ...)`。分类脚本见 §3.4，可复算。

### 3.1 总数

| 口径 | 命中 | 文件 |
|---|---|---|
| 派工书三条 grep 之和（Claude 写死） | 157 | 63 |
| 加上 `expanduser("~"), ".claude"` 拼写 | +26 | +22（hook 的端口文件常量，三份 hook_core 各 1，send_event/hook_delivery 各 2） |
| Claude 合计 | **183** | 75 |
| Codex（含注释 1 处） | **17** | 14 |
| 其中已认 `CLAUDE_CONFIG_DIR` | 10 | 8 |
| 其中已认 `CODEX_HOME` | 15 / 16 | 13 |
| 注释、文案、print 标签（不是路径逻辑，但面向用户的要跟着改） | 29 | 14 |
| 非 Python：`scripts/os-watch.sh` 2 处；`plugin/commands/*.md`、`plugin/skills/*/SKILL.md` 20 处 / 8 文件；`docs/architecture.md` 5 处；README 两种语言若干 | 另计 | |
| `tests/`（conftest 已把 HOME 指到临时目录，CLAUDE_CONFIG_DIR 未隔离，25 个文件碰它） | 27 | 7 |

### 3.2 按含义分类（代码行，不含注释与一次性脚本）

| 类 | 数 | 读/写 | 代表位置（文件:行，`×n` 为 I1 副本数） |
|---|---|---|---|
| **(a) OS 数据目录** | **51** | 库 r/w 1；端口文件 r 13；运行态 w 14；配置 r/w 4；日志 w 5；安装记录 r/w 6；守卫字符串 2 | `storage/connection.py:71`（库）；`mcp/_base.py:32`、`mcp/_autostart.py:34`（端口/调试日志）；`hook_core.py:61 ×3`、`send_event.py:19 ×2`（冻结）、`session_bootstrap.py:128/415/433 ×2`、`turn_end_guard.py:56/57/68 ×2`、`channel_unread.py:55 ×2`、`inject_subagent_context.py:15 ×2`、`pre_compact_save.py:29 ×2`、`deep_review_link.py:26 ×2`、`permission_denied_recovery.py:36 ×2`、`workflow_reminder.py:35 ×2`、`hook_delivery.py:90 ×2`（冻结）、`user_notice.py:674 ×3`、`uninstall_main_chain.py:80 ×2`；`api/debug_log.py:41`、`api/routes/settings.py:22/113`、`diagnostics.py:231`、`json_integrity.py:39`、`mcp/tools/infra.py:454`、`services/notices/install_kind.py:54`、`plugin/bootstrap.py:40`；`install.py:968`（创建 + 写 install_path.txt）；`scripts/uninstall.py:318`（`--purge-data` 删）；`scripts/check_schema_tables.py:112`、`scripts/check_usage_coverage.py:296`（机检脚本读真库）；守卫字符串 `api/routes/hooks.py:66`、`workflow_reminder.py:2363/2365`（拦截直接写数据目录的行为，也得认新位置） |
| **(b1) Claude settings.json** | 28 | r 26 / w 2 | hook 让位判断 `_tick_superseded`/`_yield_if_superseded`：`session_bootstrap.py:107/917 ×2`、`send_event.py:319 ×2`（冻结）、`channel_unread.py:525 ×2`、`inject_subagent_context.py:526 ×2`、`context_tracker.py:248 ×2`、`pre_compact_save.py:126 ×2`、`deep_review_link.py:295 ×2`、`permission_denied_recovery.py:308 ×2`、`workflow_reminder.py:2541 ×2`；`auto_install.py:151/311`（w）；`install.py:252`（w）`/505/641`；`scripts/uninstall.py:166`（w）；`api/model_discovery.py:35`；`api/language.py:32`（已认） |
| **(b2) Claude `.claude.json`** | 9 | r 6 / w 3 | `install.py:565`（w 兜底）`/586/652`（自检）；`scripts/uninstall.py:213`（w）；`context_tracker.py:24 ×2`（1M 上下文判定）；`user_notice.py:1485 ×3`（已认） |
| **(b3) 主链副本 `hooks/ai-team-os/`** | 4 | w | `install.py:196`、`auto_install.py:310/443`、`scripts/uninstall.py:154` |
| **(b4) agents / skills / commands / loop.md** | 21 | w 11 / r 10 | `install.py:338/377/415/456/622/630/636/688`、`scripts/update.py:226/245/266`、`scripts/uninstall.py:238/260/278/296`；读：`api/routes/prompt_registry.py:24`、`api/routes/agents_config.py:15`、`services/agent_template_registry.py:32`、`loop/auto_assign.py:12`、`session_bootstrap.py:400 ×2` |
| **(b5) plugins/** | 4 | r | `install.py:512`、`api/app.py:74`（dashboard-dist 缓存）、`plugin/bootstrap.py:33`、`scripts/uninstall.py:345` |
| **(b6) Claude 会话运行态 projects/ sessions/ teams/ jobs/** | 12 | r | `api/session_probe.py:36`、`api/model_discovery.py:31`、`api/agent_context.py:79`、`api/workflow_ingest.py:157`、`api/session_registry.py:41`、`api/state_reaper.py:479/1212`、`api/background_jobs.py:34`、`send_event.py:168 ×2`（冻结）、`inject_subagent_context.py:459 ×2` |
| **(b7) OS 写进 Claude 目录根的日志** | 2 | w | `pre_compact_save.py:94 ×2`（`~/.claude/compact-events.jsonl`，全仓无读者） |
| **(c) Codex home** | 14 | r | 读 `CODEX_HOME` 后兜底：`codex_rollout_usage.py:44/58`（#835cf38c 的统一解析器）、`codex_usage_recorder.py:33`、`external_exec_cmd.py:52`、`codex_observation.py:114`、`tool_catalog_codex.py:66`（先自身位置再兜底）、`notices/detectors/codex_copies.py:40`（严格存在性）、`host_versions.py:42`、`scripts/codex_adapter.py:56`、`scripts/check_codex_installed_hooks.py:89`、`scripts/codex_runtime.py:341`、`scripts/check_usage_coverage.py:300`；**空串读成当前目录**（`os.environ.get("CODEX_HOME", default)` 写法）：`services/account_monitor.py:52`、`services/local_plan_capture.py:61`、`scripts/codex_runtime.py:341` |
| **(d) 一次性脚本 / 测试** | 16 + 27 | r | 一次性脚本 11 文件 16 处：`migrate_reports.py:8`、`migrate_timestamps_utc.py:46`、`migrate_to_project_dbs.py:46/47`、`backfill_agent_session_ids.py:61/62`、`backfill_knowledge_links.py:19`、`backfill_token_usage.py:97/98`、`backfill_deep_review_sections.py:42`、`restore_purged_container_team.py:153`、`demo_seed.py:154/1306/1626/1679`、`compute_codex_golden.py:1185`（**唯一不读 `CODEX_HOME` 的 Codex 写死**）、`redact_codex_fixture.py:1186`；测试 7 文件 27 处（`test_install_mcp_registration.py`、`test_user_notice_local.py`、`test_project_summary_leaders.py` 等，用的是假 HOME） |

写的位置（最要紧，写错位置就是 2026-10-22 的事故）：(a) 的运行态与库、(b1) `install.py:252`/`auto_install.py:311`/`uninstall.py:166`、(b2) `install.py:565`/`uninstall.py:213`、(b3) 全部、(b4) 的 11 处、(b7)。

### 3.3 冻结与副本约束

- `plugin/hooks/send_event.py`、`plugin/hooks/hook_delivery.py` 的 sha256 被 I1c 冻结（`scripts/hook_entry_freeze.json`）。两者各有 (a)/(b1)/(b6) 写死共 4 处。解冻只能走核心 PR 并同批重算 CC 差分 golden、人审 diff。**在数据目录仍在旧位置（原地沿用模式）时它们照常工作**，所以解冻可以排在迁移之前、不阻塞第 1-2 批。
- `hook_core.py` 三份、其余 hook 两份（plugin/hooks 与 src/aiteam/hooks）逐字节一致（I1）；`user_notice.py` 三份。每批列出要同步的副本（§6）。
- Codex 的 `hook-trust.lock` 只哈希命令行不哈希脚本内容；改 `plugin/harness/codex/hooks/hook_core.py` 不动授信，但 I28 会 warn 到用户重跑 `codex_adapter.py update` 为止，属正常。
- 不导入 `hook_core` 的 hook（`turn_end_guard`、`deep_review_link`、`permission_denied_recovery`、`pre_compact_save`、`context_tracker`、`auto_install`）要么函数内延迟导入它，要么（若 Stop/PreToolUse 路径的导入开销实测有感）另立一个标准库的 `os_paths.py` 孪生文件并登记进 `CODEX_SUPPORT_MODULES`；由施工方实测后定。

### 3.4 复算脚本

用法 `python3 -I classify.py <仓库根>`，输出 §3.1/§3.2 的计数与 文件:行 列表。模式 = 派工书三条 grep + `expanduser\("~"\), *"\.claude"` + Codex 两种拼写；文本行判定 = 行内没有 `Path.home` / `expanduser("~` / `claude_json_path` / `_CLAUDE_CONFIG`，或以 `#` / `"""` 开头。实施时并入 I30 的扫描器，不另存脚本。

## 4. 需要维护者拍板的决策点

### 4.1 OS 数据目录放哪

| 选项 | 利 | 弊 |
|---|---|---|
| A. 固定 `~/.claude/data/ai-team-os`（机器级）+ `AITEAM_DATA_DIR` 覆盖 | 零迁移；文档、README、两套 settings.json、Codex 适配器全部不用动位置；改动最小 | **不治根因**：任何把 `~/.claude` 当作「Claude 的东西」整体搬走、备份、清理的工具（这次是多账号切换工具，下次可能是 `mv ~/.claude ~/.claude.bak`）都会再把库带走；「机器级」只是文档里的说法，物理上仍寄居在一个会话级目录里；Codex 用户的数据放在 `~/.claude` 下说不通 |
| B. 中性目录 `~/.aiteam`（+ `AITEAM_DATA_DIR` 覆盖） | 根治：不属于任何宿主，宿主的配置管理工具碰不到；与 `~/.claude`、`~/.codex` 同一层级，用户一看就懂；Windows 同样 `Path.home()/".aiteam"` | 存量用户要迁 1.65GB 的库（只能显式、可回滚）；约 60 处代码 + 文档 + 命令 Markdown 要改；过渡期两个位置并存，必须有「发现另一处有库就不新建」的守卫 |
| C. XDG（`$XDG_DATA_HOME/ai-team-os` + `$XDG_STATE_HOME/ai-team-os` + `$XDG_CONFIG_HOME/ai-team-os`） | Linux 正统；仓内已有两处用 `XDG_STATE_HOME` 的先例（`hook_core.py:329` 未读审计、`send_event_codex.py:35` 计数） | 库/运行态/配置三分，单用户本地工具没有收益却让「我的东西在哪」变成三个答案；macOS 无此惯例；Windows 要另一套映射；支持成本高于 B |

**推荐 B。** 理由：这次事故不是 bug 而是建模错误，A 只是给错误加覆盖开关。环境变量覆盖本身也是这次事故的变体来源（两个进程看到不同的环境就会分脑），所以 `AITEAM_DATA_DIR` 定位为「测试与临时工具用」，API 启动时记日志、`os_health_check` 回报，hook 发现自己解析的目录与 API 回报的不一致就发通知（§5.3）。现有 `AITEAM_DB_PATH` 保留（只覆盖库文件，测试与重启预检在用）。

**存量迁移（更新时由安装器代为完成；裁定 2026-10-22）：**

原则：搬数据的只有**用户显式触发的安装器动作**（`install.py --update`、`scripts/update.py`、`codex_adapter.py update`），三者调用同一个实现 `src/aiteam/data_dir_migration.py`（标准库 + sqlite3）；`scripts/migrate_data_dir.py` 是同一实现的手动入口与回滚入口（`--dry-run` 缺省、`--apply`、`--rollback`）。运行期（API、MCP、hook、CLI 查询）仍然只解析不搬动，所以这不违背 §2 原则 3。安装器在装机面之前先做迁移，装完的自检按迁移后的位置核对。第 1 批合入而安装器尚未接入时，存量用户什么都不用做：解析器发现 `~/.aiteam` 不存在而 `~/.claude/data/ai-team-os/aiteam.db` 是有效 SQLite 文件（检查 16 字节文件头，不看大小；事发机器上那个 0 字节残骸就是反例），进入 **legacy 模式原地沿用**。

七道安全闸，顺序执行，**任一不过即不迁**：退回 legacy 原地沿用，发 `data_dir_legacy`（params 带 `reason` 与手动命令），安装继续完成其余步骤，下一次更新再试：

| # | 闸 | 判据 | 不过时的 reason |
|---|---|---|---|
| 1 | 先看两处 | `resolve_data_dir()` 必须是 `legacy`；`conflict`（两处都有库）一律不迁，连同两处的 `PRAGMA user_version`、大小、mtime 写进通知；迁前记下旧库的 `PRAGMA user_version` 与任务数（只读 sqlite3 直读，供第 7 闸对账） | `conflict` / `not_legacy` |
| 2 | 迁移锁 | 在旧目录写 `MIGRATING`（pid、开始时间）；锁在拿到或最近一次刷新后 30 分钟内有效，跨盘复制期间每分钟刷新一次（文件 mtime），慢盘大库不会中途过期。`_autostart`、`aiteam up`、`os_restart_api` 见到有效的锁一律拒绝拉起 API；会话开始与提问的 hook（session_bootstrap、channel_unread）见锁不调 API，出一行本地 `data_dir_migrating`。其余 hook 照常运行：它们的 POST 因 API 已停而失败（投递账照记），本地记录照常写进旧目录。同盘 rename 时这些写入随目录一起走；跨盘复制时，复制完成、旧目录改名之后，由 `_carry_over_late_writes` 把复制期间写进旧目录的记录补拷到新目录。为此不再解冻任何 hook。锁已存在且有效 → 不迁。这堵住「停 API 到 rename 之间别的会话把 API 按旧路径拉起来」的窗口（只对新代码有效；旧代码进程见下文的符号链接） | `locked` |
| 3 | 停 API 并确认真停 | 先确认第 7 闸拉起的 API 跑的是本次迁移这份代码（用同一解释器与环境起子进程 `import aiteam`，不是就不迁：`--offline` 更新或手跑某个 checkout 时，已安装的另一份包会让 API 回到别的代码上）；再按 `api_port.txt` 与 pid 文件找到进程，命令行判据与 autostart 是同一份（`_autostart._is_api_command`：uvicorn 模块或 console script、`aiteam up`）并核对端口，SIGTERM（Windows `taskkill`），等到 pid 消失且端口关闭；命令行对不上 → 不杀、不迁；等不到 → 不迁 | `code_mismatch` / `api_still_running` / `pid_mismatch` |
| 4 | 没有别的进程开着库 | `lsof -t -- aiteam.db aiteam.db-wal aiteam.db-shm`（Linux 无 lsof 时扫 `/proc/*/fd`）；命中任何 pid（残留 API、MCP、sqlite3 CLI、构建脚本）→ 不迁，reason 带 pid 与命令行。Windows 没有等价手段时本闸视为不过（只通知，给手动命令） | `db_in_use` |
| 5 | WAL 归零 | `PRAGMA wal_checkpoint(TRUNCATE)` 第一列为 0，且 `-wal` 不存在或 0 字节；busy 或失败 → 不迁 | `wal_not_truncated` |
| 6 | 搬 | 同一文件系统（`st_dev` 相等）：`os.rename` 整目录到 `~/.aiteam`（原子，1.65GB 零拷贝）；跨文件系统：复制到 `~/.aiteam.partial`、比对 `aiteam.db` 的 sha256，**再探一次持库进程**（复制期间开库的进程，旧目录改名后会接着往旧库写），有就删副本、不迁；没有才改名为 `~/.aiteam`，旧目录改名 `ai-team-os.migrated-<ts>`。随后在旧位置留指向新位置的符号链接（建不了链接时才留桩目录，只含 `MOVED-TO` 与迁移记录）；建链前若旧位置已被旧代码的 hook 重新建出目录，其中的记录与日志并入新目录后再建链，里面有库则不猜、撤回并如实报告数据所在位置 | `rename_failed` / `checksum_mismatch` / `db_in_use` |
| 7 | 起 API 并对账 | 用 `mcp/tools/infra.py:_restart_spawn_on_port` 同一方式在同端口拉起 API；`/api/health` 回报 `data_dir == ~/.aiteam`、`mode == canonical`；HTTP 查任务数等于第 1 闸记下的数、`PRAGMA user_version` 不变。任一不等 → **自动回滚**：停 API、整目录改名回旧位置、删桩、解锁、按旧位置重新拉起 API，发 `data_dir_legacy` | `postcheck_failed` |

通过后删锁、在 `installs.json` 记 `data_migrated_at`，`data_dir_legacy` 不再出现。每道闸的结果逐行打印；`--dry-run` 只做第 1-5 闸的判定（第 5 闸在 dry-run 下只看 WAL 大小不 checkpoint）；`--no-migrate-data` 跳过迁移即原地沿用。迁移后两侧安装指针不用重写：settings.json 命令指向主链副本，`.claude.json` 与 config.toml 指向解释器与 HTTP 地址，都不含数据目录路径。

**插件路径单独说明。** `auto_install.py` 在每个新会话的 SessionStart 里跑：那一刻可能有别的会话（Claude、Codex、Dashboard）正在用共享 API，SessionStart 没有资格替它们停 API（第 3 闸过不了也不该去过）；两个会话同时启动还会互相竞争同一把锁；跨文件系统复制 1.65GB 也不该占着 SessionStart 的 300 秒。结论：**插件路径不迁**。`auto_install.py` 首跑发现 legacy 只做两件事：写 `data_dir_legacy`（reason=`plugin_session_start`），通知里给一条一键命令 `python3 -m aiteam.data_dir_migration --apply`（同一实现、同七道闸，用户在合适的时机在 shell 里跑）；在「什么都没有」的新机器上首跑照常创建 `~/.aiteam`（§5.2）。

**回滚：** 第 7 闸失败时自动回滚。事后回滚用 `scripts/migrate_data_dir.py --rollback`：同样先过第 2-5 闸（锁、停 API、无人开库、WAL 归零），把 `~/.aiteam` 整目录改名回旧位置、删桩、解锁、按旧位置拉起 API；跨文件系统模式下 `.migrated-<ts>` 还在，直接改名回来。代码回滚见 §9。

### 4.2 多套 Claude 配置目录：装到哪、hook 放哪、命令怎么写

**装到哪**

| 选项 | 利 | 弊 |
|---|---|---|
| i. 只装当前 `CLAUDE_CONFIG_DIR`（未设则 `~/.claude`），加 `--config-dir` 参数 | 与 Claude Code 自己的解析一致、可预测；多套就多跑几次；自检读的和写的是同一个目录 | 用户要知道自己有几套 |
| ii. 枚举全部已知目录一起装 | 一次装齐 | 没有权威清单：多账号切换工具的账号目录布局是该工具的私有实现，猜目录结构等于把别家工具的实现细节写进安装器；装进用户不想装的目录是事故 |
| iii. 每个账号单独装 | 等价于 i 跑多次 | 同 i |

**推荐 i。** 附带一份只供查询的机器级登记 `~/.aiteam/installs.json`（每次安装追加 `{host, config_dir, kind, version, at}`），`scripts/update.py --all-profiles` 与 `/os-doctor` 用它列出「OS 装在哪几套、各是什么版本」；**任何运行期判断都不依赖它**（搬走的目录登记就过期了，过期登记只能提示不能裁决）。

**hook 副本放哪、命令怎么写**

| 选项 | 利 | 弊 |
|---|---|---|
| α. 每套配置目录各一份 `<配置目录>/hooks/ai-team-os/`（当时的约定，只是把 `~/.claude` 换成实际目录），settings.json 命令写绝对路径 | 现状，改动最小；各套版本独立；E11 副本比对、卸载都按目录自洽 | 目录整体搬走时绝对路径失效（2026-10-22 12:39Z 到 12:44Z 之间 hook 全部不跑），而且默认目录若被重装，搬走的那套会**静默跑别人的副本**（现在的状态） |
| β. 机器级一份 `~/.aiteam/hooks/cc/`，所有 settings.json 都指过去 | 绝对路径永不失效；Windows 无 shell 依赖；一处比对 | 多套共享一份：插件装法下两套插件版本不同会互相覆盖；卸载一套要先确认别的套不再引用（要靠登记做裁决，违反上一条原则）；主链归属（谁装的）变成机器级问题，正是这次误报的模型 |
| γ. α 的副本布局 + settings.json 命令写 `"${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/ai-team-os/<脚本>"` | 目录搬走并设了 `CLAUDE_CONFIG_DIR` 后照常工作；哪套 settings.json 就跑哪套的副本，不串；归属标记跟副本走 | 依赖 shell 展开：文档写明 shell form 在 macOS/Linux 走 `sh -c`、Windows 走 Git Bash、没有 Git Bash 才回落 PowerShell（附录 A）；仓内 `plugin/hooks/hooks.json` 已经用 `case "$(uname -s)"` 这类 sh 语法，所以不是新依赖；PowerShell 回落下不展开（插件清单同样不支持） |

**推荐 γ，Windows 上保持绝对路径。** `install.py` 在 POSIX 写 `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` 形式，在 Windows 保持现状；`_is_our_hook`（按 `/hooks/ai-team-os/` 与脚本名匹配）与 `_MAIN_CHAIN_MARKER` 两种形式都认，安装器每次运行都重建自己的条目，旧形式自动升级。hook 侧解析自己所属的配置目录只看 `CLAUDE_CONFIG_DIR`，未设即 `~/.claude`（hook 进程继承会话环境，附录 A）。

**API 这个机器级进程怎么看配置目录（附带决策）。** API 是一个由任意会话拉起、服务所有会话的长进程，`scrub_session_env` 刻意保留 `CLAUDE_CONFIG_DIR` 给它；于是 (b6) 那 8 处（projects/、sessions/、teams/、jobs/）和 (b4) 的模板目录读的都是**拉起它的那个会话的配置目录**。多套并存时这是分脑：由搬走后目录里的会话拉起的 API，会拿那套目录去解析默认目录下会话的 projects。推荐分两步：近期（第 3 批）由 hook 的 SessionStart facts 与 MCP 请求头把会话的 `config_dir` 带给 API，API 按请求解析，自身环境只作缺省；`/os-doctor` 在此之前先报「本会话配置目录 ≠ API 的配置目录」。这一步不需要维护者单独拍板，列在这里是因为它改变 `session_env.py` 文档化的保留清单（§10 第 4 条）。

### 4.3 插件装法与源码装法怎么区分；orphan 与 /os-doctor 怎么判

**区分依据（按会话实际读的配置目录）：**

| 证据 | 位置 | 说明 |
|---|---|---|
| 插件已装且未停用 | `<配置目录>/plugins/installed_plugins.json` 有 `ai-team-os@*`，且 `<配置目录>/settings.json` 的 `enabledPlugins` 未显式 `false` | 现有 `install_kind.py` 已按配置目录读，保留 |
| 主链是谁装的 | **新**：`<配置目录>/hooks/ai-team-os/.chain-owner.json`，内容 `{"installed_by": "source"\|"plugin", "baseline": <仓库路径或插件 installPath>, "version", "installed_at"}`，由 `install.py` 与 `auto_install.py` 写 | 替代机器级的 `main-chain.json` 与 `install_path.txt` 的归属含义；跟副本走，目录搬走也不分离 |
| 源码仓库在哪 | `install_path.txt` 仍留在 OS 数据目录（它描述的是机器级事实：源码 checkout 位置），只用于找更新与基线，不再用于归属判断 | |
| `CLAUDE_PLUGIN_ROOT` | 只有从插件 hooks.json 启动的副本有它；主链副本没有 | 现状，继续只用于「我是不是插件自带副本」的让位判断，不用于判装法 |

**orphan 判定改为：** 读 `.chain-owner.json`；缺失或 `installed_by` 不是 `plugin` → **不是 orphan**（无证据不裁决，与「无标记默认插件」相反）；`installed_by == plugin` 且同一配置目录里没有已启用的 `ai-team-os@*` → orphan，提示语保持。旧安装没有 `.chain-owner.json` 时，只在**本配置目录的 settings.json 确实注册了主链**且旧 `main-chain.json` 存在且说 plugin 时才按旧规则判（一个版本后移除，§10）。数据目录缺失时先报 `data_dir_missing`，**不进入 orphan 评估**。真实的「插件被卸载」场景必须还能报出来（§7 T4）。

**/os-doctor 与各命令：** 新增 CLI `aiteam paths`（等价 `python3 -m aiteam.paths`），在会话里运行、输出 JSON：数据目录（路径、模式 canonical/legacy/missing/conflict/env、证据）、Claude（配置目录及来源 env/default、settings.json 有无主链、`.claude.json` 有无 MCP、主链副本目录、`.chain-owner.json`、插件状态）、Codex（home 及来源、hooks 目录、回执）。`os-doctor.md`、`os-hooks.md`、`os-up.md`、`os-status.md`、`os-help.md`、`os-watcher.md` 与两份 skill 不再拼写路径，改为「先跑 `aiteam paths`，按输出检查」；API 的 `/api/health` 与 `os_health_check` 回报 API 自己解析的 `data_dir` 与 `cc_config_dir`，doctor 把两边并排、不一致即报分脑。

### 4.4 Codex 侧怎么和 Claude 侧对齐、互不干扰

现状四种写法：`resolve_codex_home()`（#835cf38c，`or` 兜底 + `.expanduser().resolve()`）、`codex_copies.codex_home()`（字面、严格存在、不存在抛 NoDataError）、`tool_catalog_codex.codex_home()`（env → 自身位置 `<home>/hooks/<adapter>/` → 默认）、`os.environ.get("CODEX_HOME", default)`（空串变当前目录，3 处）。

| 选项 | 利 | 弊 |
|---|---|---|
| a. 各模块各自对齐到 `resolve_codex_home()` | 改动小 | 服务侧函数，hook 用不了（hook 只能用标准库且不能 import aiteam）；四种语义还是没有一个文本定义 |
| b. 解析段进 `hook_core.py`，`codex_home(self_locate=False)` 与 `cc_config_dir()`、`os_data_dir()` 同段同约定；服务侧经 `aiteam.paths` 用；`codex_copies` 的严格存在性做成外层包装 | 一份定义、三份字节副本、I1 对钉；Claude/Codex 用同一个 `os_data_dir()`，天然「只在数据目录相交」 | 要同步三份副本；Codex 安装副本在用户重跑 update 前 I28 会 warn |

**推荐 b。** 语义：`CODEX_HOME` 非空 → `Path(value).expanduser()`（与 #835cf38c 一致；Codex 原生不做 `~` 展开，但 shell 通常已经展开，做了不伤）；空串 → 视为未设置；hook 侧 `self_locate=True` 时在 env 未设的情况下按 `__file__` 位于 `<home>/hooks/<adapter>/` 推断（`tool_catalog_codex` 现有规则，保留）；再无则 `~/.codex`。收口 4 处：`account_monitor.py:52`、`local_plan_capture.py:61`、`codex_runtime.py:341`（空串）、`compute_codex_golden.py:1185`（不读 env）。

**互不干扰的边界：** `install.py` 与 `scripts/uninstall.py` 只碰 Claude 配置目录；`codex_adapter.py` 只碰 Codex home（它已声明「共享 API、数据库和 Claude 文件未改动」）；两者都不动对方；共同依赖只有 OS 数据目录与 API 地址。Codex 适配器的三份 hook（`user_notice.py` 副本等）通过同一个 `os_data_dir()` 写 `notice-local.codex.jsonl`，§7 T5 用「两套都设了环境变量时两边写进同一个目录」钉住。

## 5. 目标架构细节

### 5.1 解析器接口（`hook_core.py` 路径段；`aiteam/paths.py` 仅 re-export）

```
LEGACY_DATA_DIR  = ~/.claude/data/ai-team-os        # 全仓唯一允许拼写此路径的地方（I30）
DEFAULT_DATA_DIR = ~/.aiteam

resolve_data_dir() -> DataDirResolution(path, mode, evidence)
    mode: "env"       AITEAM_DATA_DIR 非空（原样，expanduser）
          "canonical" ~/.aiteam 存在
          "legacy"    ~/.aiteam 不存在且 LEGACY/aiteam.db 是 SQLite 文件
          "conflict"  两处都有 SQLite 文件（返回 canonical，evidence 列两处大小与 mtime）
          "missing"   都没有；evidence 列查过的候选（含 <cc_config_dir>/data/ai-team-os）
os_data_dir() -> Path            # mode 为 missing 时抛 DataDirMissing(evidence)
cc_config_dir() -> Path          # CLAUDE_CONFIG_DIR 非空 → expanduser；否则 ~/.claude
cc_global_config() -> Path       # cc_config_dir()/.claude.json
cc_main_chain_dir() -> Path      # cc_config_dir()/hooks/ai-team-os
codex_home(self_locate=False) -> Path
```

约束：标准库；不 mkdir；不 resolve 符号链接（事发当天就是用符号链接止血的，canonical 可以是链接）；所有函数无缓存（hook 是短进程，API 启动时解析一次存到 app.state）。`DEFAULT_DB_URL` 从 import 期常量改为首次取引擎时求值（`repository.py:8738`、`deps.py:693`、`app.py:35/135/142`、`routes/system.py:331` 五处用到），否则 `import aiteam.storage.connection` 在没有数据目录的机器上就炸。

### 5.2 创建权

允许 mkdir 的入口：`install.py` 第 5 步、`plugin/hooks/auto_install.py` 首跑、`scripts/codex_adapter.py install`、`scripts/migrate_data_dir.py`、显式 `AITEAM_DATA_DIR` / `AITEAM_DB_PATH`。创建前必须跑一遍 `resolve_data_dir()`：legacy → 不创建、打印迁移提示；conflict → 不创建、打印两处；missing 且 `<cc_config_dir>/data/ai-team-os/aiteam.db` 存在（2026-10-22 事故的形态）→ 不创建、提示 `migrate_data_dir.py --from <路径>`；只有「什么都没有」才创建。`connection.py:26 _migrate_old_db_if_needed`（启动时从 cwd 自动拷库）与这条原则冲突，已裁定移除（§10 第 1 条）。

**搬迁权**是创建权名单减去 `auto_install.py`：只有 `install.py --update`、`scripts/update.py`、`codex_adapter.py update` 与手动入口 `scripts/migrate_data_dir.py` 能调用 `src/aiteam/data_dir_migration.py`（§4.1 七道闸）；`_autostart`、`os_restart_api`、`aiteam up`、hook、CLI 查询永不搬动，并且见到 `MIGRATING` 锁就拒绝拉起 API。

插件首会话的时序：MCP autostart 是惰性的（首次工具调用才拉 API），`auto_install.py` 在 SessionStart 就跑完首跑创建；两者顺序由 §7 T3 的插件变体钉住。

### 5.3 通知

| id | 级别 | 渲染 | 触发 | 文案要点 |
|---|---|---|---|---|
| `data_dir_legacy` | info，每安装一次；reason 变化时再发 | API | mode == legacy；params：`reason`（§4.1 七道闸的不过原因、`plugin_session_start` 或空 = 安装器尚未接入）、`command`（手动一键命令） | 数据仍在旧位置、为什么这次没迁、不迁也能用、下一次更新会再试、手动命令 |
| `data_dir_migrating` | info | local | 旧目录或 `~/.aiteam` 旁存在未过期的 `MIGRATING` 锁 | 迁移进行中（开始时间、pid）；本会话暂不连 API |
| `data_dir_missing` | action | **local**（hook 自渲染，API 不可用也能出） | mode == missing 且本配置目录 settings.json 注册了主链，或 Codex 回执存在 | 没有新建空库；候选位置与检查结果；`aiteam paths --locate` |
| `data_dir_conflict` | action | local + API | mode == conflict，或 env 覆盖目录没库而 canonical/legacy 有库 | 两处大小、mtime、`PRAGMA user_version`；当前用哪一处 |
| `data_dir_split` | action | local | hook 解析到的目录 ≠ API `/api/health` 回报的 | 两个路径；重启 API 的命令 |
| `orphan_main_chain` | 不变 | local | §4.3 新规则 | 不变 |

### 5.4 安装器

- `install.py --config-dir PATH`（缺省读 `CLAUDE_CONFIG_DIR`）；开头打印「本次安装目标：配置目录 X、数据目录 Y（模式）」。
- 顺序：解析 → **迁移**（§4.1 七道闸；只在 `--update`、`scripts/update.py`、`codex_adapter.py update` 里做，`--no-migrate-data` 跳过；首次安装没有旧数据可迁）→ 装机面 → 自检。自检按迁移后的位置核对，并复述每道闸的结果；闸不过时安装不算失败，只是 legacy 原地沿用。
- MCP：先 `claude mcp add-json --scope user`，子进程环境显式带上目标 `CLAUDE_CONFIG_DIR`；随后**读 `cc_global_config()` 验证**，没写到就直写该文件；自检同样读它，并在 `~/.claude.json` 也有条目时提示「另一套配置目录也注册了」（无害，只提示）。
- hooks：副本到 `cc_main_chain_dir()`；命令按 §4.2 γ；写 `.chain-owner.json`；settings.json 写 `cc_config_dir()/settings.json`。
- agents/skills/commands/loop.md：全部按 `cc_config_dir()`。
- 第 5 步：按 §5.2 创建权规则；`install_path.txt` 写进数据目录；追加 `installs.json`。
- `scripts/update.py`、`scripts/uninstall.py`、`plugin/hooks/auto_install.py` 同规则；`auto_install.py` 对 legacy 只通知不迁（§4.1 插件路径）；`uninstall.py --purge-data` 删的是解析出的数据目录并打印路径、大小与 `PRAGMA user_version` 让用户确认（§10 第 5 条）。

## 6. 分批实施计划

每批独立可合入、可回滚；每批末尾跑 `bash scripts/check_invariants.sh` 与全量测试。「同步副本」列是 I1 要求逐字节一致的文件，改一处必须同步所有副本。

| 批 | 内容 | 同步副本 | 完成判据 |
|---|---|---|---|
| **0 机检先行** | I30 落地（§8），棘轮值 = 当前计数；解析器矩阵单测与 `aiteam.paths` 身份测试先写红 | 无 | I30 绿（计数 ≤ 棘轮）；新单测红 |
| **1 解析器与数据目录** | `hook_core.py` 路径段（含 `codex_home`）；`aiteam/paths.py`；(a) 类 51 处改用解析器（冻结的 `send_event.py`/`hook_delivery.py` 除外，legacy 模式下它们仍正确）；`DEFAULT_DB_URL` 延迟求值；创建权规则进 `install.py:968`、`auto_install.py`、`codex_adapter.py`；`data_dir_*` 五条通知；`/api/health` 回报；`src/aiteam/data_dir_migration.py`（七道闸、自动回滚）与手动入口 `scripts/migrate_data_dir.py`（`--dry-run`/`--apply`/`--rollback`）；`MIGRATING` 锁进 `_autostart`、`os_restart_api`、`aiteam up` 与 hook；`aiteam paths`；Codex 口径收口 4 处 | `hook_core.py` ×3；`user_notice.py` ×3；`session_bootstrap.py`、`turn_end_guard.py`、`channel_unread.py`、`inject_subagent_context.py`、`pre_compact_save.py`、`deep_review_link.py`、`permission_denied_recovery.py`、`workflow_reminder.py`、`uninstall_main_chain.py` 各 ×2 | §7 T1、T2、T5、T6、T8（手动入口变体）、U1、U4 绿；存量机器（维护者的机器）不迁移照常工作并收到 `data_dir_legacy`（reason 为空）；棘轮降到约 100 |
| **2 安装器与配置目录（含代为迁移）** | `install.py`、`scripts/update.py`、`scripts/uninstall.py`、`plugin/hooks/auto_install.py` 按 §5.4；`install.py --update`、`scripts/update.py`、`codex_adapter.py update` 在装机面之前接入 `data_dir_migration`（§4.1 七道闸）；`auto_install.py` 插件路径只通知 + 一键命令；`.chain-owner.json`；`installs.json`（含 `data_migrated_at`）；orphan 新规则；`install_kind.py` 改读 `.chain-owner.json`；`installed_copies.py` 基线不变；(b1)-(b5) 中安装器侧全部 | `session_bootstrap.py` ×2；`auto_install.py` 单份（PLUGIN_ONLY） | §7 T3、T4、T7、T8 绿；维护者的机器走真实用户路径 `install.py --update` 完成迁移（§11 同批），再对两套配置目录各跑一次 `install.py --config-dir`，`claude mcp list` 在搬走后目录的会话里看到 ai-team-os；orphan 不再误报、插件移除场景仍报 |
| **3 hook 让位判断与冻结入口（已完成，2026-10-26）** | **冻结入口**：`send_event.py` 的端口文件、teams 目录与 `hook_delivery.py` 的账本目录先行改走路径段（入口只从 hook_core 取 `DataDirMissing`/`api_port_file`/`cc_config_dir` 三个名字，等价测试钉死取的面，`_get_api_url` 恢复逐字比对）；本批随 (b1) 再解冻一次 `send_event.py`，CC 差分 golden 重算后只有入口哈希与 MANIFEST 摘要两处变化，载荷、处理状态与三表快照不变，I1c 重冻。**(b1)**：9 个业务 hook 的让位判断收成一段逐字节相同的共享块 `_main_chain_commands` + `_yield_if_superseded`（`test_yield_sentinel.py` 对钉整段），读本会话配置目录的 settings.json（经 hook_core 的 `cc_config_dir`，共享块只取这一个名字，send_event 的取用面因此不变）；`session_bootstrap` 的 `_tick_superseded` 改为在同一块之上判断，去掉了第二份「找主链命令」。**`context_tracker.py`**：读 `.claude.json` 推断 1M 窗口的那段在默认值改为 1M 后结果恒为 1M，属死代码，整段删除（不再读全局配置），不改走 `cc_global_config()`。**`pre_compact_save.py`**：离线记录写进 OS 数据目录，没有数据目录就不记；`inject_subagent_context` 的 teams 目录、`session_bootstrap` 的 loop.md 兜底同样改走配置目录。**API 按请求解析配置目录**：hook_core 路径段加请求级绑定（`bind_cc_config_dir`，ContextVar；`own_cc_config_dir` 是进程自己的），`cc_config_dir()` 在 API 里回答本请求所属会话的目录；`aiteam/api/cc_config_scope.py` 负责绑定：MCP stdio 服务端每次调用带 `X-CC-Config-Dir`（百分号编码），hook 在 `/api/notices/pending` 的 facts 里带 `config_dir`（会话开始与每次提问），hook 事件按自带的 `transcript_path` 推出所在配置目录（会话开始的几个事件互相竞速、API 重启后都靠它），API 记住会话 id → 目录，请求没有说明时用 API 自己的。不属于任何请求的工作（state reaper 的周期）不绑定，读 API 自己的目录。(b6) 的 projects/sessions/teams/jobs、(b4) 的 agents 模板目录（四处收成 `agent_template_registry.template_dirs()` 一处）、(b5) 看板的插件缓存与 model_discovery 的 settings.json 都改读 `cc_config_dir()`；`/api/health` 的 `cc_config_dir` 报 API 自己的。**归属标记**：`.chain-owner.json` 加过渡规则收进 hook_core `main_chain_owner()`（记录优先；没有记录时 `install_path.txt` 判源码、否则看旧 `main-chain.json`），orphan 判定、`uninstall_main_chain.py`（只清插件装的链，归属不明拒绝）、`installed_copies.py`、`config_change.py` 共用；`aiteam paths` 显示归属记录、按它判出的装法与 installs.json 各目标的最新登记。**不做**：`scripts/compute_codex_golden.py` 保持原样（2026-10-26 维护者同意不重算 I19：维护者机器上的 Codex 原生会话在 golden 冻结后被导入改写，导入记录 32→51，旧采集器已复算不出现有 golden，19 个生产口径键漂移；重算等于拿改写后的数据换基准；写死的默认值只在开发者重算答案时用，在 I30 棘轮里作已知剩余项保留）。**已知限制**：state reaper 的周期扫描只看 API 自己的配置目录（与改前相同），其它配置目录里的 Agent Teams 成员与会话不在它的视野内；要覆盖需让扫描遍历已报告过的目录，留作后续。 | `context_tracker.py`、`send_event.py`、`pre_compact_save.py` 等 9 个业务 hook 与 `uninstall_main_chain.py` 各 ×2；`hook_core.py`、`user_notice.py` 各 ×3 | I1c 新哈希；§7 T2 仍绿；等价测试逐字比对；两套配置目录的会话同时打到一个 API 各读各的（`tests/integration/test_config_dir_per_session.py`） |
| **4 文档、命令、脚本** | 8 份命令/skill Markdown 改为调用 `aiteam paths`；`docs/architecture.md` 路径表；README 两种语言（I6 只数数字，路径句子手改）；CLAUDE.md「刻意决策」加一条「OS 数据目录是机器级中性目录，运行期不创建」；(d) 11 个一次性脚本默认值改 `aiteam.paths`；`scripts/os-watch.sh`；`docs/user-notice-design.md` 第 16 行与 §760 的「不用 CLAUDE_CONFIG_DIR 做隔离」注明已解除 | 无 | I30 棘轮 = 0（只剩三份 hook_core） |
| **5 发版验收** | 按 os-release 的用户生命周期验收：新装；从 v1.15.2 升级且迁移发生在升级里（七道闸逐行可读）；闸不过的形态（§7 T8 的手工复现）退回原地沿用；插件路径只通知；升级后已装副本生效。CHANGELOG 写清「数据目录新位置、更新时代为迁移、七道闸与退回条件、手动与回滚命令、插件用户的一键命令」；与 4459b1e、Ultrafast 计价、测试隔离一起发 public | 无 | 升级验收里重启 API 后任务数不变；杀会话恢复实证 |

## 7. 测试计划

环境约束沿用 `tests/conftest.py`：HOME 已指向临时目录，子进程 HOME 不等于临时目录会被拒绝；新增的用例**必须同时设 `CLAUDE_CONFIG_DIR` 与 `CODEX_HOME`**，这是本设计解除 docs/user-notice-design.md 第 16 行限制后的第一批用例。替身不得比生产宽松：安装器用例直接调用 `install.py` 的真实函数（`register_hooks`、`register_global_mcp`、`verify_installation`），不 mock 路径函数；跨进程断言：API 用 `LiveAPI` 模式（`tests/integration/test_user_notice_hooks_live.py:49`）另起子进程，用 HTTP 查库，不看内存对象。

### 7.1 端到端复现 2026-10-22 的事故（`tests/integration/test_config_dir_relocation.py`）

| 用例 | 步骤 | 修复前（红的理由） | 修复后断言 | 反向验证（撤回哪一行必须重新变红） |
|---|---|---|---|---|
| **T1 旧用户 + 目录被搬走** | 假 HOME 下用真实安装函数装进 `~/.claude`（数据目录在旧位置），起 API，建 1 个任务，停 API；`mv ~/.claude <假 HOME>/profiles/one/config`，设 `CLAUDE_CONFIG_DIR`；跑 `session_bootstrap.py`（SessionStart payload）；再起 API | hook 在 `~/.claude/data/ai-team-os/` 新建目录并写文件；发 `orphan_main_chain`；API 起来且任务数 0 | `~/.claude/` 下**没有新建任何文件**；本地通知是 `data_dir_missing` 且 evidence 含 `<config>/data/ai-team-os/aiteam.db`；没有 `orphan_main_chain`；API 以非零退出并打印同样的候选；`~/.aiteam` 不存在 | `hook_core.os_data_dir` 改回 `Path.home()/".claude"/...`：第一条断言红；`_orphan_main_chain` 的默认值改回 `"plugin"`：第三条红 |
| **T2 已迁移用户 + 目录被搬走** | 同上但数据在 `~/.aiteam`；搬目录、设变量；跑 SessionStart；重启 API | 不适用（新形态） | 无通知；API 起来后 HTTP 查到任务数 = 1；hook 解析的 `data_dir` == `/api/health` 的 | 解析器去掉 `~/.aiteam` 分支：任务数断言红 |
| **T3 安装器在 CLAUDE_CONFIG_DIR 下** | PATH 前置一个假 `claude`（记录 argv 与环境后退出 1）；设 `CLAUDE_CONFIG_DIR=<tmp>/profile`；调 `register_global_mcp`、`register_hooks`、`verify_installation`；变体 B：假 `claude` 成功并把条目写进 `$CLAUDE_CONFIG_DIR/.claude.json`；变体 C（插件）：`auto_install.py` 首跑在空机器上创建数据目录、在 legacy 存在时不创建 | 条目写到 `~/.claude.json`；自检 `[OK]`；`$CLAUDE_CONFIG_DIR/.claude.json` 没有 | 条目在 `$CLAUDE_CONFIG_DIR/.claude.json`，`~/.claude.json` 不存在；假 `claude` 收到的环境里 `CLAUDE_CONFIG_DIR` 是目标目录；settings.json 在 `$CLAUDE_CONFIG_DIR/`，24 条命令含 `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hooks/ai-team-os/`；`.chain-owner.json` 为 `source`；自检输出引用目标目录；变体 B 不走兜底 | `install.py` 的 `cc_global_config()` 改回 `Path.home()/".claude.json"`：第一条红 |
| **T4 orphan 真假** | 四个矩阵：owner=plugin 且插件已移除；owner=plugin 且插件启用；owner=source；无 owner 文件（旧安装） | 第四格误报 | 只有第一格报；第四格在存在旧 `main-chain.json{installed_by:plugin}` 且本目录 settings.json 注册了主链时才报（过渡规则）；数据目录缺失时四格都只报 `data_dir_missing` | 默认值改回 `"plugin"`：第四格红 |
| **T5 两套变量同时设** | `CLAUDE_CONFIG_DIR=<tmp>/profile`、`CODEX_HOME=<tmp>/codex`；把 Codex 适配器 hooks 拷到 `<tmp>/codex/hooks/ai-team-os-observer/`；分别跑 CC `session_bootstrap.py` 与 Codex `session_bootstrap_codex.py`；再分别用 `CODEX_HOME` 未设（自身位置推断）与 `CODEX_HOME=""` 跑 `tool_catalog_codex.codex_home()`、`aiteam.paths.codex_home()` | 三种写法结果不同；空串得到 cwd | `notice-local.cc.jsonl` 与 `notice-local.codex.jsonl` 在**同一个**解析出的数据目录；未设 → 自身位置 `<tmp>/codex`；空串 → `~/.codex`（假 HOME 下） | `account_monitor.py:52` 改回 `get("CODEX_HOME", default)`：空串断言红 |
| **T6 分脑检测** | API 以 `AITEAM_DATA_DIR=A` 起；hook 环境无覆盖、解析到 B；跑 SessionStart | 无检测 | 本地通知 `data_dir_split`，含 A 与 B | 删除 health 对比：红 |
| **T7 从 v1.15.2 形态升级，数据在旧位置** | 假 HOME 下按 v1.15.2 布局装：数据与库在 `~/.claude/data/ai-team-os`，settings.json 24 条绝对路径命令，无 `.chain-owner.json`；LiveAPI 子进程起在该库上，经 HTTP 建 3 个任务；记下 `PRAGMA user_version`；以子进程跑真实入口 `install.py --update`（依赖安装步骤用现有的跳过开关） | 不适用（新形态；v1.15.2 没有迁移） | 升级过程停掉了旧 API（旧 pid 不在、期间端口关过）；`~/.aiteam/aiteam.db` 存在且 `PRAGMA user_version` 同升级前；旧位置只剩桩（`MOVED-TO` 指向 `~/.aiteam`）与迁移记录，**没有** `aiteam.db`；API 已重新起来，`/api/health` 的 `data_dir == ~/.aiteam`、`mode == canonical`，HTTP 任务数 = 3；`MIGRATING` 锁已删；settings.json 命令变 `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` 形式，`.chain-owner.json` 为 `source`；再跑一次 SessionStart 不再发 `data_dir_legacy` | 去掉安装器对迁移的调用：「库在 `~/.aiteam`」红；去掉第 3 闸（不停旧 API）：旧 API 在 rename 后按路径新开连接，旧位置重新出现 `aiteam.db`，「旧位置只剩桩」红 |
| **T8 故障注入：别的进程开着库** | 同 T7 布局；另起一个子进程模拟残留的旧服务：打开 `aiteam.db` 保持连接并等待信号，收到信号后**按原路径重新连接**并插一行任务（那次事故的故障形态）；跑 `install.py --update`；之后给子进程发信号；变体：用手动入口 `scripts/migrate_data_dir.py --apply` 代替安装器 | 不适用 | 不迁：`~/.aiteam` 不存在，旧位置完整，没有 `.migrated-*`、没有桩、没有残留锁；安装其余步骤照常完成（settings.json、副本、MCP、`.chain-owner.json`）；API 重新起来仍是 legacy 模式，HTTP 任务数 = 3；`data_dir_legacy` 的 params 含 `reason=db_in_use`、持有进程 pid 与命令行、手动命令；子进程按路径重连后那一行落在旧库里（HTTP 任务数变 4），旧位置始终只有一个库 | 去掉第 4 闸（lsof/等价）：rename 照样成功，子进程按原路径重连在旧位置新建空库，「旧位置始终只有一个库」红；去掉第 1 闸的 conflict 判定：预置两处都有库的变体「不迁」红 |

### 7.2 单元

- **U1 解析器矩阵**：`AITEAM_DATA_DIR`（未设/空串/设）× `~/.aiteam`（无/有/桩）× 旧位置（无/0 字节文件/有效库/桩）→ 期望 mode；每格断言测试目录树前后快照一致（**不 mkdir**）。
- **U2 副本一致**：`aiteam.paths.os_data_dir is aiteam.hooks.hook_core.os_data_dir`（防止有人在包里再写一份）；三份 `hook_core.py` 已由 I1 管。
- **U3 命令匹配**：`_is_our_hook` 对绝对路径形式与 `${CLAUDE_CONFIG_DIR:-$HOME/.claude}` 形式都为真，对用户自己的 `hooks/ai-team-os/` 外脚本为假。
- **U4 迁移实现**：`data_dir_migration` 的七道闸逐闸可单独制造不过（conflict、残留锁、pid 命令行对不上、lsof 命中、WAL busy、校验和不符、对账不等），每种都断言「没动旧目录、锁已清、reason 正确」；同文件系统（rename）与跨文件系统（复制 + sha256 + 改名）两条路径；第 7 闸失败触发自动回滚后目录树与迁移前逐字节一致；`--rollback` 同上；`MIGRATING` 锁存在时 `_autostart` 与 `os_restart_api` 拒绝拉起 API。

### 7.3 发版前人工验收

1. 在 `CLAUDE_CONFIG_DIR` 指向别处的会话里 `claude mcp list` 看到 `ai-team-os`；`/os-doctor` 报配置目录 = 该目录、数据目录模式 = legacy（迁移前）/ canonical（迁移后）、API 与 hook 一致。
2. 原生 `claude` 会话同样通过；两套各自的 `.chain-owner.json` 都是 `source`。
3. 走真实用户路径 `install.py --update` 完成迁移（不手工跑脚本），输出里七道闸逐行可读；重启 API 后 `task_list_project` 计数与迁移前一致；`kill -9` 一个会话再开，通知与任务墙完好（dogfooding 判据是故障注入）。
4. 从 v1.15.2 升级：`install.py --update` 在装机面之前完成迁移；手工复现 T8（另开一个 `sqlite3` 保持连接）时退回原地沿用、`data_dir_legacy` 的 reason 可读；不再出现 `orphan_main_chain`。
5. 插件路径：在隔离 HOME 里走 marketplace 安装（docs/user-notice-design.md §760 的环境），SessionStart 首跑只发 `data_dir_legacy`（reason=`plugin_session_start`）不迁；按通知里的一键命令迁移后同样通过第 3 条。

## 8. 新机检 I30 规格（加法）

```
# ── I30: 宿主/数据目录路径只许在 hook_core.py 拼写（事故: 2026-10-22 外部工具把 Claude 配置目录整体搬走，
#        库离家、MCP 落错配置、orphan 误报；任务 5160c89c / b7d973fd）──
```

- **扫描范围**：`src/aiteam`、`plugin`、`scripts`、`install.py` 的 `*.py`；`plugin/commands/*.md`、`plugin/skills/**/*.md`；`scripts/*.sh`。不扫 `tests/`（判据与对照实现）、`docs/`、README、CHANGELOG。
- **模式**（任一命中即计 1 行）：`Path.home() / ".claude"`、`expanduser("~/.claude`、`"~/.claude/`、`.claude.json`、`expanduser("~"), ".claude"`、`Path.home() / ".codex"`、`expanduser("~/.codex`、`"~/.codex`、`".aiteam"`、`$HOME/.claude`、`~/.aiteam`。注释与文案一并计入（文案本来就要改，棘轮负责过渡）。
- **允许名单**：`plugin/hooks/hook_core.py`、`src/aiteam/hooks/hook_core.py`、`plugin/harness/codex/hooks/hook_core.py`，三份各自的命中数必须相等（I1 已保证字节相等，这里是防扫描器失效的交叉校验），且每份 ≤ 4 行（`LEGACY_DATA_DIR`、`DEFAULT_DATA_DIR`、`~/.claude`、`~/.codex` 四个常量）。
- **棘轮**：脚本内 `I30_MAX=<n>`；名单外命中数 > n → 红；< n → warn「把 I30_MAX 降到 <实际数>」（防止棘轮只升不降而失效）；= n → 绿。第 0 批写入 n = 当前计数（约 220，含注释、Markdown 与 sh），每批合入时降，第 4 批后 n = 0。
- **失效防护**：扫描范围缺失、模式零命中且名单文件不存在 → 红（与 I29 同款，防假绿）。
- **配套单测**（不在 sh 里）：U1 的「不 mkdir」断言，是「运行期不创建数据目录」原则的机检形态；创建权入口列表写在 `aiteam/paths.py` 文档字符串里，U1 用 `monkeypatch` 把 `os.mkdir`/`Path.mkdir` 替换成抛错来证明解析器不碰它们。

## 9. 回滚方案

| 批 | 代码回滚 | 数据/配置回滚 |
|---|---|---|
| 0 | revert | 无 |
| 1 | revert；`DEFAULT_DB_URL` 恢复常量 | 安装器尚未接入迁移，没有自动搬过数据；若用户手动跑过 `migrate_data_dir.py --apply`，先 `--rollback` 再 revert；`data_dir_*` 通知的本地记录是追加文件，旧代码忽略未知 catalog_id |
| 2 | revert；重跑旧版 `install.py` 把 settings.json 命令重写回绝对路径（安装器每次重建自己的条目） | 已被安装器迁移的机器：`scripts/migrate_data_dir.py --rollback`（同一实现，先过第 2-5 闸，整目录改名回旧位置、删桩、按旧位置拉起 API）；第 7 闸失败的机器已自动回滚，无需处理；`.chain-owner.json`、`installs.json` 对旧代码是惰性文件；旧代码仍读 `main-chain.json`（第 2 批不删它，只是不再写） |
| 3 | revert；`scripts/hook_entry_freeze.json` 随 revert 回到旧哈希 | 无 |
| 4 | revert | 无 |
| 5 | revert | 无（迁移在第 2 批的安装器里，回滚见第 2 批；两侧安装指针不含数据路径，无需重装） |

任何一批回滚后 `check_invariants.sh` 必须仍绿：I30 的棘轮值随代码一起回退。

## 10. 减法清单（2026-10-22 裁定：第 1-3、5-7 条批准；第 4 条第 3 批按「不剥」处理）

按「拿不准算加法还是减法，按减法办」列出，裁定结论已上墙（任务 #5160c89c memo）：

1. **移除 `connection.py:26 _migrate_old_db_if_needed`**：启动时从 cwd 自动拷 `aiteam.db` 到数据目录（新库 <10KB 就拷）。与「运行期不创建/不搬数据」冲突，而且 10KB 阈值对那次事故的空库形态是盲的。
2. **`_orphan_main_chain` 的无标记默认值从 `plugin` 改为不判**：这是把一个误报检测改严（少报），归到减法以防争议。配套过渡规则见 §4.3。
3. **一个版本后停止读机器级 `main-chain.json`**：第 2 批只停写不停读；停读是删一条兼容路径。
4. **`session_env.py` 保留清单里的 `CLAUDE_CONFIG_DIR`**（第 3 批定：不剥，保留清单不变，不算减法）：API 按请求解析配置目录之后，它仍是 API 自己的配置目录，即请求没有说明所属会话时（看板、reaper 周期）的缺省值；剥掉它会让这类读取一律落到默认目录。
5. **`scripts/uninstall.py --purge-data` 的删除对象**从固定路径改为解析出的目录：删除路径变了，按「删数据前问能不能重建」的规矩，脚本要先打印路径、大小与 `PRAGMA user_version` 再要确认。
6. **解冻 `send_event.py` 与 `hook_delivery.py`**（I1c）：不是放宽机检，但走的是核心 PR + golden 重算 + 人审的文档化流程，列在这里让维护者知情并排期。
7. **`docs/user-notice-design.md` 第 16 行 / §760 的「验收不用 CLAUDE_CONFIG_DIR 隔离」**：那是基于「hook 都写死 `Path.home()/.claude`」的决定，本设计的前提变了，建议在第 4 批标注解除并允许用它隔离。

加法（不需要裁定，直接做）：I30；`data_dir_*` 五条通知；`MIGRATING` 迁移锁；`data_dir_migration` 的七道闸与自动回滚；`aiteam paths`；`.chain-owner.json`；`installs.json`；U1-U4、T1-T8。

## 11. 迁移后的残留（不在代码里处理）

- 从「配置目录被整体搬走」的形态升级上来的机器，搬走后的配置目录里可能还留着旧的 `data/ai-team-os`（指向新位置的链接，或早期留下的空库）。`resolve_data_dir()` 会把它当作候选，一直报 conflict。确认数据已在新位置（`aiteam paths`）后，手工删掉它；`aiteam paths --locate` 会列出这类残留。
- 搬走后的配置目录里没人引用的旧主链副本，在对该目录跑一次 `install.py --config-dir <该目录>` 时会被刷新，settings.json 也会指向它。
- 默认位置的全局配置 `~/.claude.json` 里的 MCP 条目，是给不设 `CLAUDE_CONFIG_DIR` 的会话用的，保留。
- 第 2 批对维护者机器的真实迁移，走真实用户路径 `install.py --update`，不手工跑脚本；跑之前先停下其他会话对 API 的使用（第 3、4 闸会核实），输出里七道闸逐行留档，作为发版验收证据。

## 附录 A：Claude Code 文档核对（2026-10-22，code.claude.com）

| 问题 | 文档说法 | 本设计的依赖 |
|---|---|---|
| `CLAUDE_CONFIG_DIR` 带走什么 | 「All settings, session history, and plugins are stored under this path」；claude-directory 页列出 settings.json、skills/、commands/、agents/、projects/、plugins/（含 installed_plugins.json）、sessions/、jobs/、daemon/、.credentials.json 都在其下 | `cc_config_dir()` 统一这些子目录 |
| `.claude.json` 是否随之移动 | **未记载**（全站只写 `~/.claude.json`；debug-your-config 让用户把 `~/.claude.json` 与 `~/.claude` 的文件拷进新目录但没写目标） | 以事发机器上的实测为准（`$CLAUDE_CONFIG_DIR/.claude.json` 75KB 持续更新）；安装器**写后验证**而不假设；§7.3 第 1 条人工核对 `claude mcp list` |
| `teams/` 位置 | 未记载 | (b6) 按 `cc_config_dir()`，与其余运行态目录同规则 |
| hook 命令经不经 shell | 无 `args` 即 shell form：macOS/Linux `sh -c`，Windows Git Bash，无 Git Bash 回落 PowerShell；shell 展开变量 | §4.2 γ 的 `${CLAUDE_CONFIG_DIR:-$HOME/.claude}`；Windows 保持绝对路径 |
| hook/MCP 子进程继承环境 | hook 进程继承父环境；stdio MCP 默认继承 shell 环境；`CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` 的剥除清单里含 `CLAUDE_CONFIG_DIR`，说明默认在子进程环境里 | hook 侧 `cc_config_dir()` 只读环境变量 |
| `CLAUDE_PLUGIN_ROOT` | shell form 与 exec form 都导出 `CLAUDE_PROJECT_DIR`、`CLAUDE_PLUGIN_ROOT`、`CLAUDE_PLUGIN_DATA`；settings.json 里的 hook 有没有它未记载 | 继续只用于「我是不是插件自带副本」 |
| `claude mcp add-json --scope user` 写哪 | user scope 存 `~/.claude.json`；设了 `CLAUDE_CONFIG_DIR` 时写哪未记载 | 安装器写后验证，验证失败才直写 |
