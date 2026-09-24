# 启动时正式版本提醒

> 部分已被取代：提醒文案、安装方式判定与会话去重已并入统一的用户提示账本，见 `docs/user-notice-design.md`（E09）。下文「会话去重」一节与「不引入通用 notices 表」一句不再适用。

## 目标与边界（2026-09-23 裁定）

Codex / Claude Code 启动会话时发现 AI Team OS 新正式版，用户看到一行中英双语提醒和对应安装方式的更新命令；模型同时收到用户所见内容、运行版本、正式版本、发布页及用户要求更新后的操作说明。提醒不执行 Git、不安装依赖、不重启服务。统一语言设置由独立的 Dashboard / language API 写集交付，本模块只消费该契约，不扩展通用通知账本。

## 数据与入口

- API 按需查询公开仓库 `CronusL-1141/AI-company` 的 GitHub latest Release，仅接受非草稿、非预发布的 `vMAJOR.MINOR.PATCH`；不把 master 提交差异当作正式发布。
- `/api/releases/latest` 提供当前运行版本、最新正式版、检查状态、缓存时间、固定仓库发布链接、`language`、短 `notice` 与模型专用 `additional_context`。当前版本来自运行进程；磁盘代码更新不等于服务已更新。
- 请求参数：`host=cc|codex|system`、`cwd`、`fallback_language`、`installation=cc-plugin|cc-source|codex`。`host=codex` 强制选择 Codex 指引，避免误给 Claude 安装路径。兼容原 `language` / `Accept-Language` 作为回退候选，均不能覆盖 Dashboard 手选值。
- 成功缓存六小时，失败退避十五分钟，旧成功结果保留并标为过期。缓存仅存公开版本元数据；切换语言或安装方式不重新请求远端。
- 同 API 实例内并发合并一次检查，持久缓存可跨 API 重启读取。网络总预算一秒，失败不阻断会话。缓存不是跨多个 API 进程的分布式锁。
- 不注入远端 Release 标题、正文或提供的 URL；发布链接由固定仓库和严格校验的版本 tag 本地拼接。

## 通知语言

统一入口 `GET /api/settings/language?host=...&cwd=...&fallback_language=...` 返回 `{mode,effective,source}`；`PUT /api/settings/language` 保存 `{mode: follow|zh|en}`。版本 API 调用同一个异步 `resolve_language`，重新读取当前手选值。

优先级：

1. API 侧 Dashboard 手选 `zh` / `en`。
2. 跟随模式中，CC 使用项目 `.claude/settings.local.json` → `.claude/settings.json` → 用户 `settings.json` 的 `language`。用户目录遵从 `CLAUDE_CONFIG_DIR`，否则为 `~/.claude`。
3. Codex 使用原生语言设置（仅在存在已核实的公开字段时接入）；当前核查的 Codex 配置与 SessionStart 输入 schema 未提供此字段，因此使用系统首选语言。Codex 不读取 CC 设置。
4. 未配置宿主语言时使用系统首选语言，最后英文。macOS Hook 优先以只读 `defaults` 获取 `AppleLanguages` 首项（每进程一次，0.2 秒预算）；失败回落 `LC_ALL` / `LC_MESSAGES` / `LANGUAGE` / `LANG`，最后 Python locale。中文 locale / CC 中文语言名称为中文，其余语言回落英文。

Hook 先读取统一语言 API；接口不可用时本地按宿主规则回退。版本接口仍须可用才有新版证据；语言接口不可达不会凭空生成版本通知。废止 `AITEAM_NOTICE_LANGUAGE`，不再读取该覆盖变量。不修改真实宿主配置，不翻译旧启动简报。

Codex 能力依据本地官方 `openai/codex` 源码 `0a2eb4696c26ac33204bcd255721ab30220a4774` 的 `codex-rs/core/config.schema.json`、`core/src/config/` 与 `hooks/schema/generated/session-start.command.input.schema.json`；这些可用来源未发现原生 language/locale 字段，不推断 Desktop 私有字段。

## 用户行与模型上下文

用户行单行、典型正式版本约八十字符，缓存结果加简短缓存标记。示例：

| 安装方式 | 中文用户行示例 |
| --- | --- |
| CC 插件 | `[AI Team OS] v1.15.0: claude plugin update ai-team-os；重启。` |
| CC 源码 | `[AI Team OS] v1.15.0: git pull --ff-only && python3 -m pip install -e .；重启。` |
| Codex | `[AI Team OS] v1.15.0: python3 scripts/codex_adapter.py update（先更新源码和依赖）` |

英文版使用相同命令和等价短语。URL 只进入模型 `additionalContext`，不进入用户行。模型上下文明确这是用户已看到的通知，并明确不得自动更新；用户要求更新后：

- CC 插件执行 `claude plugin update ai-team-os`，完成后重启 CC。
- CC 源码先确认原安装 checkout 无未提交改动，再快进拉取、用系统解释器更新依赖，按既有源码安装流程同步已安装 Hook；协调 API 所有者重启，重启 CC。
- Codex 先确认原安装 checkout 干净，执行 `git pull --ff-only`、`python3 -m pip install -e .`、`python3 scripts/codex_adapter.py update`；原 stdio / 仅 Hook 安装使用 `update --hooks-only` 保留 MCP 配置。执行 `status`，按已有授权和所有权协调 API 重启后重连 Codex；不停止其他会话使用的共享服务。

两端使用顶层 `systemMessage` 给用户，`hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: ...}` 给模型。Codex 严格限于官方 `session-start.command.output.schema.json` 认可的字段，不输出其他宿主扩展字段。没有新版时保留原启动简报和 compact 恢复上下文。

## 会话去重

> 已被取代：去重改由用户提示账本的送达记录完成（`docs/user-notice-design.md` §5.6 与 E09：每个宿主冷却 24 小时，只计已确认的送达），`release-notices/` 标记目录不再写入，也不删除。

`XDG_CACHE_HOME/ai-team-os/release-notices/<cc|codex>/<SHA256(session_id)>`，未设置 XDG 时使用 `~/.cache`。只为有效新版通知原子创建一个零字节 marker；只使用会话哈希作文件名，不将会话 ID 拼进路径。CC 两副本共享 CC namespace，Codex 单独 namespace。

同一宿主、同一会话最多通知一次，即使 Hook 新进程、resume、compact 或重复 startup；新会话可再次提醒。不按语言或版本重置已有会话的标记，不设到期日，避免长期会话恢复时重弹。只保留标记，不保存会话文本、URL、版本历史，不引入通用 notices 表或后台清理任务（已被取代：`docs/user-notice-design.md` §5.2 引入了 `notices` 与 `notice_deliveries` 两表；仍不设后台清理任务）。

无有效会话 ID 时不写公共 marker：startup 可显示，resume / compact 不显示。标记目录不可写时跳过用户通知，原简报照常。标记在写 stdout 前预占，保证并发不重复；若进程恰在预占后、输出前终止，本会话可能漏一次提醒。这是最多一次的去重边界，不宣称用户送达确认。

## 兼容迁移与验证边界

旧 API 不支持新接口、离线、限流、坏缓存均不阻断启动；未知状态不宣称“已是最新版”。旧接口返回含 URL 的旧长用户行不显示，避免违反新输出约定。旧客户端首次获得功能仍需先更新 Hook；重载 MCP 不会安装新文件。两份 CC hook 逐字节同步；Codex 沿用现有注册与信任面，不增加 handler。

本轮覆盖正式版本比较、坏数据、断网退避、跨实例缓存、语言切换与服务端手选优先、安装命令、远端正文不进入提示、单 JSON 双通道、严格字段、跨模块/跨子进程/并发去重及缓存不可写降级。测试使用临时数据库与缓存，不触碰真实宿主设置、不启动付费模型会话、不改变发布状态。

源码协议与测试通过不代表已安装 Hook 或正在运行 API 已更新，也不代表新一轮 Codex 原生 UI 实际送达。CC 已完成的 TUI 可见性验证不重复施工；Codex 安装、授信和真实新会话显示作为独立验收层报告。
