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

调用 `task_list_project()`（不传参即用当前活跃项目）。返回按 short/mid/long
三档分组、组内按 score 排序的任务，直接照此顺序展示——这个排序就是"接下来该
做什么"的答案，不要自行重排。

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

## 输出格式

### 任务墙
```
## 项目任务墙（short / mid / long）

### short
| 优先级 | 任务ID | 标题 | 状态 | 分配给 |
|--------|--------|------|------|--------|
| critical | 3f2a… | 修复登录 500 | running | backend-dev |
```

### 任务详情
```
## 任务 3f2a…

状态: running   优先级: critical   归属: dev-team
依赖: 无        分配给: backend-dev

### 进展记录
- [progress] 已复现，定位到 session 中间件
- [decision] 采用 A 方案，理由…
```

## 注意

- 所有输出使用中文
- 状态用中文标记：pending=待处理, running=进行中, blocked=被阻塞,
  completed=已完成, failed=失败
- 任务墙默认是精简视图（`view: "compact"`）——那是有意裁剪，不是字段缺失；
  要全字段传 `fields="all"`，要单个任务的完整信息用 `task_status`
