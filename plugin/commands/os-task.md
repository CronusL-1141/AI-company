---
name: os-task
description: AI团队任务管理 — 查看任务墙、创建任务、看任务详情
---

# /os-task — 任务管理

帮助用户管理 AI Team OS 的任务墙。

## 用法

- `/os-task` — 查看当前项目的任务墙
- `/os-task <team_id|团队名>` — 只看某支队的任务墙
- `/os-task new <描述>` — 把一件事放上任务墙
- `/os-task <task_id>` — 查看单个任务详情与进展记录

## 操作流程

### 无参数：看任务墙

调用 `task_list_project()`（不传参即用当前活跃项目），先原样展示返回开头的
`digest`：全墙统计（未关条数按状态 × 期限、近 7 天新建与关闭、停滞与沉底计数）、
最近动静 5 条、待办最优先 5 条、中长期待办之首。用户要看更多再展开 `wall`：
按 short/mid/long 分组，每组先是一页待办（按分数，同分先上墙的在前；`not_shown`
是这一页没列出的待办条数），再是全部进行中、阻塞、失败的任务。

只看一支队时传 `task_list_project(team_id="<队名或ID>")`。

### new 模式：把任务放上墙

1. 确认两件事（用户已给出就不要再问）：任务描述、目标团队。
   团队用 `team_list()` 取；只有一支活跃队就直接用它。
2. 调用 `task_create(...)`；若要立刻挂到某支队名下并带优先级/时间档，
   用 `task_run(team_id=..., description=..., priority=..., horizon=...)`。
3. **任务上墙 ≠ 有人在做**：OS 不会自己执行任务。要推进就用 CC 的 `Agent(...)`
   派人，并在 prompt 里写明 task_id，让其用 `task_memo_add` 回写进展。

### 查看模式：任务详情

1. `task_status(task_id)` — 状态、归属、依赖、结果。
2. `task_memo_read(task_id)` — 历史进展记录（接手前必读）。
3. 想看完整时间线：`task_execution_trace(task_id)`；要连带耗时/步数统计就
   `task_execution_trace(task_id, include_stats=True)`。

## 注意

- 状态用中文标记：pending=待处理, running=进行中, blocked=被阻塞,
  completed=已完成, failed=失败
