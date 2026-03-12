# AI Team OS — 开发协调

## 当前阶段: Milestone 2 — 可视化管理

## M1 回顾: ✅ 已完成并发布
- commit bee43d8, 78/78测试通过, CP1-CP5全部验证
- 已push到 GitHub master

## M2 架构决策（5专家调研确认）
- PostgreSQL 16 + pgvector + Redis 7 via Docker
- FastAPI + WebSocket频道订阅
- Vite + React 19 + React Router v7 + Zustand + TanStack Query + shadcn/ui
- Mem0 SDK直连PG+pgvector（非独立服务）
- 双后端共存（SQLite默认 / PostgreSQL可选）

## M2 任务进度

| 任务 | 负责人 | 状态 |
|------|--------|------|
| T1 Docker+PostgreSQL+Redis基础设施 | storage-engineer | 🔵 进行中 |
| T2 记忆系统重构(MemoryBackend Protocol) | memory-engineer | ⏳ 待开始 |
| T3 Broadcast编排模式 | graph-engineer | 🔵 进行中 |
| T4 FastAPI+WebSocket API层 | api-engineer | ⏳ 待开始 |
| T5 Dashboard骨架(Vite+React+shadcn) | frontend-engineer | ⏳ 待开始 |
| T6 Dashboard: 总览+团队页 | frontend-engineer | ⏳ 待开始 |
| T7 Dashboard: 任务看板+事件日志 | frontend-engineer-2 | ⏳ 待开始 |
| T8 Dashboard: 设置页 | frontend-engineer | ⏳ 待开始 |
| T9 Human-in-the-Loop审批节点 | graph-engineer | ⏳ 待开始 |
| T10 集成测试+CI | qa-engineer | ⏳ 待开始 |

## Phase执行计划
```
Phase 1 (当前): T1 + T3 并行
Phase 2:        T2 + T4 并行 (依赖T1)
Phase 3:        T5 (依赖T4)
Phase 4:        T6 + T7 并行 (依赖T5)
Phase 5:        T8 + T9 + T10
```
