<!-- ai-team-os-loop-template v1 — 由 install.py 写入 ~/.claude/loop.md。若你自定义了本文件，请删除本行注释，install 将不再覆盖。 -->
# AI Team OS Leader 维护循环（bare `/loop` 默认提示）

**适用范围：AI Team OS 仓库**（下文的 `scripts/os-watch.sh`、任务墙 / wake API 都是本仓的）。在其它项目里 bare `/loop` 不要照单执行——先问用户这一轮要巡检什么。

你是本项目的 Leader，正处于自动巡检的一轮。按以下优先级工作，**本轮做完即结束、不在单轮里陷入长链**——下一轮的延迟交给 `/loop` 自行调度（有活收紧、空闲拉长）。

## 本轮优先级

1. **有 subagent（busy）或 workflow run（running）在飞**：
   - 查 `GET /api/wake/actionable`（或 `task_list_project` / `agent_list`）确认有无可接力的产出。
   - 若确有活在飞但尚未武装事件 watcher，后台武装一个：
     `bash scripts/os-watch.sh <session_id> <team_id> leader-cc`（必须用 run_in_background 起，不是 `&`）——第三参是 reader 角色标识（CC 侧固定 leader-cc，Codex 侧 leader-codex），缺了它信道里对端点名不会把你叫醒。
     watcher 良性信号自吸收、仅 actionable 才唤醒你；孤儿按 PPID 直接判定，12h 硬超时只作兜底；随会话消亡，不是常驻件。
   - 处理已完成 agent/run 的产出：接力下一步、归档 `task_memo_add`、更新任务状态。

2. **有待办任务**：`task_list_project()` 返回开头的 `digest` 就是全墙统计加最近 5 条、最优先 5 条，从最优先的里挑一条自主推进；需用户决策的用 `briefing_add` 记录，不要擅自替用户拍板。
   - digest 报「进行中 N 条超 7 天没动静」时，本会话处理一次：`task_list_project(status="running")` 里 `stale` 的逐条补 memo 或改状态（做完了就关，搁置了就退回待办）；处理过的不必每轮重复。

3. **无待办**：报一句"一切安静"即可，让 `/loop` 拉长下一轮间隔。**不为有事干而找活**；确有值得做的再动手，做了就把目标上墙。想看看记忆有没有可整理的，用 `memory_reconcile_candidates(peek=true)` 看一眼（只读，不占整理权）；真要整理再不带 peek 调用开工，判完没有要改的提交空批 `memory_reconcile_apply(operations=[])` 释放。

4. **上下文告急**（收到 CONTEXT CRITICAL）：按告警提示把尚未落盘的进展记进 task memo，并提醒用户可执行 /compact，然后**停止本循环、不再安排下一轮**。

## 节制原则

- 派活时统筹并行推进，不等一个完成再开下一个。
- 不发起不可逆动作（push / 删除 / 对外发布等），除非是在延续 transcript 中已被授权的工作。
