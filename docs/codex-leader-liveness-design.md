# Codex Leader 自动休眠与恢复

本实现覆盖精确 owned Codex session 容器的心跳离线、API 启动对账和自动关队。仅处于 busy 的精确 owner Leader 可走 completed 矛盾态；普通配置不能注入资格，未知原因 offline 不猜测恢复。实现入口见 `services/agent_liveness.py`、`storage/repository.py`、`api/deps.py`、`api/state_reaper.py` 与 `api/hook_translator.py`（路径均相对 `src/aiteam/`）。

## 自动路径

资格保存在 agent config 的保留字段，含自动原因、发生时间和 nonce。心跳理由为 heartbeat_timeout；启动对账的过期 waiting→offline 为 startup_reconciliation。精确 Codex owner 的启动 busy→waiting 通过 agent/team 双快照 CAS 完成；CC 和 child 保留既有启动行为。启动快照遇到新活动、人工下线或重绑时冲突即停止，不回退通用写入。

30 分钟自动关队使用 team 身份/config/更新时间和完整成员身份/活性/集合快照 CAS。waiting 的精确 owner 在同一事务真实置 offline，并取得 session_auto_close 资格；已有自动 offline owner 保留资格。未知原因 offline owner 不补授资格。team 记录关闭时间、关闭 nonce、Leader ID 和 offline nonce。其余成员置 offline 并撤资格，不取得恢复父容器的权限；子线程随后活动不能自行开启父队，仍可能保持 offline，这是现有终结边界。

新原生活动必须非 replay，来源时间有时区、在既有 60 秒窗口内，且晚于资格和关闭时间。completed 队恢复在同一事务内复核角色、项目、native/session、owner、完整或历史短队名、Leader 指针和双 nonce；任一行失败均回滚。active 队保持旧恢复宽度：agent 身份/状态/资格快照及 team id/active 门槛，不因无关队 metadata 变化冲突。SessionStart 也可恢复 active 队中带资格的 offline root，同时保留尾部 force 采集；root 的 parent_thread_id 必须为空，child 仍可指向父 session。

## 明确操作与兜底（本轮裁定）

显式 update_team(status=completed) 在同一事务、写锁内先以当前 team+owner SQL 谓词将旧精确 Codex root 归一 offline/current_task=None并撤资格，再更新队。这也覆盖同值 completed 以及同次 config=None/owner/Leader 指针变化；活动时间和用量不动，CC 与 child 状态不归一。普通 metadata 写仅撤 root 资格，不置 offline；非 active 状态写仍按旧契约撤全体成员资格。

因此 busy→completed 留下 offline/completed；completed→明确 busy 留下 busy/completed，可由 reaper 对精确 owner 双快照 CAS 开队。旧 reaper busy 快照若遇到后来的显式 completed，agent/team 条件失配而拒绝，不能翻转人工关闭。历史 busy/completed 无法追溯操作意图，按既有矛盾态兜底语义允许精确 owner 纠正；busy child/身份不符不得开父队。有效 CC owner 优先走旧兜底，兼容真实 CC 出生的 native tool ID 为空、team 项目尚未绑定。当前 session pointer 指向真实 Codex root 时，即使队名/项目失配也保守保护，纠正仍须完整身份一致。否则 roster 中精确 Codex root 的 native/session、项目、完整/短队名证据可在 config/pointer 被移除后继续保护，busy child 不能冒用。若现有 pointer/kind 和 roster 的项目/native/队名证据都不足（例如多项身份被同时移除），属于容器身份已不可观察，无法无标记还原历史意图，不扩大猜测或追加意图框架。本批不引入跨模块写入意图框架。

人工同值 offline、同值 completed、配置写入、身份变更与 SessionEnd 对当前数据库行撤资格，不用旧 ORM config 覆盖；config=None 保持清空语义，SQL/JSON null 在快照中与读出的空 dict 对齐。

## 审计与验证

自动关队对真实匹配成员的 SQL 更新落 agent.updated，包含自动原因、team ID、关闭 nonce 与结果；reaper 成功 info、CAS 冲突 debug。显式 completed 的 root 归一也落独立原因事件。恢复/纠正仅真实开队一次时落一条 team.auto_revived。原生活动恢复还经 agent.status_changed 广播触发团队刷新，reaper 矛盾态纠正仅落队审计；项目摘要仍有现有 30 秒轮询延迟。

原始 _hook_replay 及 _codex_completion_replay 均无活性权。完成 outbox 和发送端超限降级 _metadata 都带后者：保存完成关联和受限输出，活动事件用 observed_at，不刷新存活；同次 PreToolUse 已提供活性。

测试跨真实 handle_event、48 路并发、启动对账、人工操作两种顺序与竞态、旧快照、真实 sender/HTTP/临时 DB、真实子进程 force 用量采集。关键守卫撤回必须变红并按 SHA 还原，既有失败证据保留。历史丢资格行仅通过受审私有程序显式选择的两个固定目标处理；部署方须先让包含启动对账修复的新实现进入实际 API，再在两次原生工具调用之间生成 fresh plan 并立即执行，15 分钟活动门槛不构成泛化恢复权限。
