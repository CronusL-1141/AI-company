# 多部门架构可行性研究报告

> 研究者: org-architect | 日期: 2026-03-20
> 状态: Completed

## 1. 研究目标

评估AI Team OS从单层 `Leader → Workers` 演进为多层部门架构 `CEO → 部门Lead → Workers` 的可行性，基于Claude Code (CC) 平台的实际能力边界，给出分级方案。

### 目标架构
```
董事长(用户)
  └── CEO(Leader)
        ├── QA Lead → QA Workers
        ├── R&D Lead → Researchers
        ├── Engineering Lead → Developers
        └── 其他部门...
```

---

## 2. CC平台能力边界分析

### 2.1 当前CC Team机制（实测 + 官方文档）

| 特性 | 状态 | 说明 |
|------|------|------|
| 一个session一个team | **硬限制** | "One team per session: a lead can only manage one team at a time" |
| 嵌套team | **不支持** | "No nested teams: teammates cannot spawn their own teams or teammates. Only the lead manages the team." |
| Teammate成为Lead | **不支持** | "The session that creates the team is the lead for its lifetime. You can't promote a teammate to lead" |
| 跨team SendMessage | **不支持** | SendMessage只能寻址同team成员名，无跨team寻址机制 |
| Teammate间直接通信 | **支持** | 同team内任意agent可SendMessage给任意其他agent |
| 共享任务列表 | **支持** | 同team内所有agent共享task list，支持依赖和自领取 |
| Plan Mode审批 | **支持** | Lead可要求teammate先plan再实施，Lead审批后放行 |

### 2.2 团队配置存储结构（实测）

```
~/.claude/teams/{team-name}/
  ├── config.json     # 团队配置：members数组，每个含agentId/name/model/prompt
  └── inboxes/        # 消息信箱
```

config.json关键字段：
- `leadAgentId`: 固定为 `team-lead@{team-name}`
- `members[]`: 扁平数组，无层级字段
- 无 `parent_team` / `sub_teams` / `department` 等层级概念

### 2.3 关键限制总结

**CC的设计哲学是"扁平协作"而非"层级管理"**：
1. 一个session只能lead一个team
2. Teammate不能spawn子team
3. 没有跨team通信原语
4. 没有团队间共享任务或共享channel（社区Feature Request #30140已被标记duplicate）

---

## 3. 多Agent框架对比参考

### 3.1 CrewAI — Flows + Crews

| 概念 | 说明 | 对应我们的概念 |
|------|------|----------------|
| Crew | 一组有角色的Agent协作执行任务 | Team |
| Flow | 编排多个Crew的有状态工作流 | 部门间协调层 |
| Process.hierarchical | Manager Agent分配+验证任务 | Leader |
| Process.sequential | 任务按顺序传递 | 流水线 |

**关键洞察**: CrewAI用Flow作为Crew之上的编排层，Crew是执行单元，Flow是控制流。多个Crew通过Flow协调，而非Crew嵌套。

### 3.2 AutoGen — 嵌套Group Chat

| 概念 | 说明 |
|------|------|
| GroupChat | 多Agent共享消息线程 |
| GroupChatManager | LLM选择下一个发言者 |
| 嵌套GroupChat | "可以将group chat嵌套为层级结构，每个参与者本身是一个递归的group chat" |

**关键洞察**: AutoGen理论上支持嵌套，但官方文档缺乏实现指导，实际使用多为扁平结构。

### 3.3 LangGraph — 图编排

| 概念 | 说明 |
|------|------|
| StateGraph | 节点(Agent)+边(转移)+状态 |
| SubGraph | 图可嵌套为子图 |
| Reducer | 合并并发更新 |

**关键洞察**: LangGraph原生支持SubGraph嵌套，一个"部门"可以是一个SubGraph，由上层图编排。这是最接近多部门架构的技术路径。

---

## 4. 通讯网络设计选项分析

### 4.1 四种模式对比

| 模式 | 描述 | 优势 | 劣势 | 适用场景 |
|------|------|------|------|----------|
| **集中式** | 所有通讯经CEO转发 | 最简单，CEO全知全能 | CEO成瓶颈，延迟高，CEO上下文爆炸 | Agent数量<5 |
| **分层式** | Worker→Lead→CEO，同部门内直通 | 减轻CEO负担，部门内高效 | 跨部门通讯仍需经CEO | 部门职责清晰时 |
| **广播式** | 事件总线，各部门订阅 | 松耦合，可扩展 | 噪音大，难以精确通讯 | 事件驱动为主 |
| **混合式** | 日常走分层，重要事件走广播 | 灵活，兼顾效率和全局感知 | 实现复杂度最高 | 大规模组织 |

### 4.2 推荐：混合式

理由：
- 日常工作：分层通讯（Worker→Lead→CEO），同部门内Worker可直接交流
- 重大事件：通过OS EventBus广播，所有Lead和CEO自动收到
- 跨部门协作：通过OS Meeting系统，临时拉跨部门成员讨论

---

## 5. 可行性评估——三级方案

### 方案A：CC原生方案（利用现有CC机制）

**实现思路**：在单一CC team中模拟部门

```
CC Team "project-x"
  ├── team-lead (CEO角色)
  ├── qa-lead (QA部门Lead)
  ├── qa-worker-1
  ├── qa-worker-2
  ├── eng-lead (工程部门Lead)
  ├── eng-worker-1
  └── eng-worker-2
```

**具体做法**：
1. 所有agent在同一个CC team中
2. 通过agent命名约定区分部门：`qa-lead`, `qa-worker-1`, `eng-lead`等
3. CEO(team-lead)分配任务时按"部门"指定
4. 部门内agent可直接SendMessage
5. 通过CLAUDE.md/spawn prompt注入"你属于QA部门，你的直属上级是qa-lead"

**能实现到什么程度**：
- [x] 逻辑上的部门分组 — 通过命名约定+prompt注入
- [x] 部门内直接通讯 — CC原生支持同team内SendMessage
- [x] CEO全局协调 — team-lead天然是全局协调者
- [ ] ~~部门Lead独立管理~~ — Lead无法spawn/shutdown自己的worker
- [ ] ~~真正的层级权限隔离~~ — 所有agent权限相同
- [ ] ~~跨部门隔离~~ — 所有agent都能看到所有task和message

**评估**：
| 维度 | 评分 | 说明 |
|------|------|------|
| 实现难度 | 低 | 只需命名约定和prompt工程 |
| 部门隔离 | 弱 | 逻辑隔离，无强制隔离 |
| 可扩展性 | 中 | 受限于CC单team ~5-6 agent建议 |
| 部门自治 | 弱 | Lead无管理权限，依赖CEO |
| Token效率 | 低 | 所有agent在同一team，广播成本高 |

**推荐规模**: 1 CEO + 2-3个部门 × 1-2个worker = 5-9 agents

---

### 方案B：OS增强方案（通过AI Team OS的MCP/Hook/EventBus扩展）

**实现思路**：CC单team扁平 + OS层模拟多部门

```
CC层（扁平）:
  CC Team "project-x": team-lead, qa-lead, eng-lead, researcher

OS层（层级）:
  ┌─────────────────────────────────────────────┐
  │  OS Department Manager (新模块)              │
  │  ┌──────────┬──────────┬──────────────────┐ │
  │  │ QA Dept  │ Eng Dept │ Research Dept    │ │
  │  │ lead: qa │ lead: eng│ lead: researcher │ │
  │  │ scope: QA│ scope:Eng│ scope: Research  │ │
  │  └──────────┴──────────┴──────────────────┘ │
  │                                               │
  │  OS TaskWall: 任务按部门标签分区               │
  │  OS Meeting: 支持部门内/跨部门两种模式         │
  │  OS EventBus: 部门级事件订阅                   │
  └─────────────────────────────────────────────┘
```

**具体做法**：

#### B.1 新增Department数据模型
```python
# 新增到types.py
class Department(BaseModel):
    id: str
    team_id: str              # 所属团队
    name: str                 # 部门名称：QA, Engineering, Research
    lead_agent_id: str | None # 部门Lead的agent_id
    member_agent_ids: list[str] = []
    config: dict = {}

# Agent扩展
class Agent(BaseModel):
    ...
    department_id: str | None = None  # 所属部门（可选）
```

#### B.2 新增MCP工具（约5-6个）
```
department_create(team_id, name, lead_agent_id)
department_list(team_id)
department_assign_agent(department_id, agent_id, role)
department_briefing(department_id)    # 部门级简报
department_task_view(department_id)   # 部门任务视图
```

#### B.3 EventBus增强
```python
# 部门级事件过滤
await event_bus.emit(
    "task.completed",
    source=f"department:{dept_id}",  # 部门作用域
    data={...}
)
# Lead只接收本部门事件
```

#### B.4 TaskWall增强
```python
# 任务增加department字段
class Task(BaseModel):
    ...
    department_id: str | None = None

# taskwall_view支持按部门筛选
taskwall_view(team_id, department_id="qa-dept")
```

#### B.5 Meeting增强
```python
# 支持部门内会议 + 跨部门会议
meeting_create(team_id, topic, department_id="qa-dept")  # 部门会议
meeting_create(team_id, topic, cross_department=True)     # 跨部门
```

**能实现到什么程度**：
- [x] 部门数据模型和元数据 — DB持久化
- [x] 部门级任务视图 — TaskWall按部门分区
- [x] 部门级事件过滤 — EventBus按department scope
- [x] 部门内/跨部门会议 — Meeting增强
- [x] 部门级简报 — department_briefing聚合本部门信息
- [x] Dashboard部门视图 — 前端按department_id分区展示
- [ ] ~~部门Lead自主spawn worker~~ — CC限制
- [ ] ~~通讯强制隔离~~ — CC的SendMessage无法限制

**评估**：
| 维度 | 评分 | 说明 |
|------|------|------|
| 实现难度 | 中 | ~2周工作量，5-6新MCP工具+DB迁移+前端 |
| 部门隔离 | 中 | OS层逻辑隔离，CC层无隔离 |
| 可扩展性 | 中高 | OS层无限制，CC层仍受agent数量约束 |
| 部门自治 | 中 | Lead通过OS工具管理本部门任务/会议，但不能管理agent生命周期 |
| Token效率 | 中 | CC层仍是扁平team，但OS层的scope减少不必要信息获取 |

**推荐规模**: 1 CEO + 3-5个部门 × 1-3个worker = 7-20 agents（CC层只放Lead，Worker按需spawn）

---

### 方案C：独立平台方案（如果不受CC限制）

**实现思路**：完全脱离CC，直接调用LLM API

```
┌──────────────────────────────────────────────────┐
│              Agent OS Runtime                     │
│  ┌──────────────────────────────────────────────┐│
│  │          Organization Manager                ││
│  │  ┌────────────────────────────┐             ││
│  │  │      CEO Agent             │             ││
│  │  │  (Anthropic Messages API)  │             ││
│  │  └─────────┬──────────────────┘             ││
│  │            ├──────────┬──────────┐          ││
│  │  ┌────────▼─┐ ┌──────▼───┐ ┌───▼────────┐ ││
│  │  │ QA Dept  │ │ Eng Dept │ │ R&D Dept   │ ││
│  │  │  Lead    │ │  Lead    │ │  Lead       │ ││
│  │  │  ├ W1    │ │  ├ W1    │ │  ├ W1       │ ││
│  │  │  ├ W2    │ │  ├ W2    │ │  └ W2       │ ││
│  │  │  └ W3    │ │  └ W3    │ │              │ ││
│  │  └──────────┘ └──────────┘ └──────────────┘ ││
│  └──────────────────────────────────────────────┘│
│  ┌──────────────────────────────────────────────┐│
│  │          Communication Bus                   ││
│  │  - 部门内直通（同进程消息队列）               ││
│  │  - 跨部门路由（Lead↔Lead或经CEO）             ││
│  │  - 全局广播（重大事件）                       ││
│  │  - 共享Channel（持久化+全序）                 ││
│  └──────────────────────────────────────────────┘│
│  ┌──────────────────────────────────────────────┐│
│  │          Tool Execution Layer                ││
│  │  - File I/O (aiofiles)                       ││
│  │  - Terminal (asyncio.subprocess)             ││
│  │  - Git (pygit2 / subprocess)                 ││
│  │  - Web (httpx)                               ││
│  └──────────────────────────────────────────────┘│
└──────────────────────────────────────────────────┘
```

**关键能力**：
1. **真正的层级管理**: 部门Lead可spawn/shutdown自己的worker
2. **通讯隔离**: 消息路由器强制执行层级规则
3. **独立上下文管理**: 每个Agent的上下文窗口独立管理，支持checkpoint/恢复
4. **多模型支持**: CEO用Opus，Worker用Sonnet/Haiku，按需分配
5. **成本控制**: 精确控制每个Agent的API调用预算

**评估**：
| 维度 | 评分 | 说明 |
|------|------|------|
| 实现难度 | 极高 | 3-6个月全职开发，需重建CC的核心能力（代码编辑、终端、上下文管理） |
| 部门隔离 | 强 | 完全可控 |
| 可扩展性 | 极高 | 无平台限制 |
| 部门自治 | 强 | Lead有完整的agent生命周期管理权 |
| Token效率 | 高 | 精确控制每个Agent的模型和上下文 |

---

## 6. 决策矩阵

| 维度 (权重) | 方案A CC原生 | 方案B OS增强 | 方案C 独立平台 |
|-------------|-------------|-------------|---------------|
| 实现难度 (0.25) | 9 | 7 | 2 |
| 部门隔离 (0.15) | 3 | 6 | 9 |
| 可扩展性 (0.20) | 4 | 7 | 9 |
| 部门自治 (0.15) | 2 | 5 | 9 |
| Token效率 (0.10) | 3 | 5 | 8 |
| 当前可用性 (0.15) | 9 | 7 | 1 |
| **加权总分** | **5.45** | **6.35** | **5.80** |

**结论：方案B（OS增强方案）是当前最佳选择**

---

## 7. 推荐实施路径

### Phase 1: 方案A快速验证（1-2天）
- 在现有CC team中用命名约定模拟部门
- 测试"qa-lead"能否有效协调"qa-worker-1"
- 验证prompt注入的部门身份是否有效影响agent行为
- **成果**: 验证部门概念是否对协作效率有实际提升

### Phase 2: 方案B核心实施（2周）
- 新增Department数据模型 + DB迁移
- 5-6个部门管理MCP工具
- TaskWall按部门分区
- Meeting支持部门scope
- EventBus部门级过滤
- **成果**: OS层完整的部门管理能力

### Phase 3: Dashboard部门视图（1周）
- 组织架构图展示
- 部门级Agent状态面板
- 部门级任务看板
- 跨部门通讯流可视化
- **成果**: 用户可视化管理多部门

### Phase 4: 方案C长期准备（持续）
- 参考 `docs/standalone-agent-os-feasibility.md`（另一位researcher正在研究）
- 关注CC平台演进（嵌套team、共享channel等社区需求进展）
- 逐步积累独立运行时能力

---

## 8. 前端展示设计考虑

### 8.1 部门视图（推荐首选）
```
┌─────────────────────────────────────────────────────┐
│  项目: AI Team OS    团队: os-work    循环: #3      │
├──────────────┬──────────────┬───────────────────────┤
│  QA部门       │  工程部门     │  研究部门             │
│  Lead: qa-l  │  Lead: eng-l │  Lead: researcher     │
│  ┌─────────┐ │  ┌─────────┐ │  ┌────────────────┐  │
│  │ W1 工作中│ │  │ W1 空闲 │ │  │ W1 工作中       │  │
│  │ W2 空闲  │ │  │ W2 工作中│ │  └────────────────┘  │
│  └─────────┘ │  │ W3 工作中│ │                       │
│              │  └─────────┘ │                       │
│  任务: 3/5   │  任务: 7/12  │  任务: 2/4            │
├──────────────┴──────────────┴───────────────────────┤
│  跨部门活动流                                        │
│  10:23 [eng-w2] Edit src/api/routes.py              │
│  10:22 [qa-w1] Bash: pytest tests/                  │
│  10:21 [研究-w1] WebSearch: memory architecture     │
└─────────────────────────────────────────────────────┘
```

### 8.2 组织架构图
- 树形图展示CEO→部门Lead→Workers
- 每个节点显示状态颜色（绿=idle, 黄=busy, 红=error）
- 点击节点展开详情
- 连线显示最近通讯频率（粗细）

### 8.3 通讯流视图
- 类似Slack的channel列表
- 全局channel + 每部门channel
- 消息类型图标区分（任务/讨论/事件/告警）

---

## 9. 风险与缓解

| 风险 | 影响 | 缓解策略 |
|------|------|----------|
| CC移除team功能或大幅改变API | 方案A/B失效 | 方案B的OS层隔离了CC依赖，迁移成本可控 |
| Agent数量增加导致token成本爆炸 | 财务 | Worker用Sonnet/Haiku；Lead和CEO用Opus |
| 部门间信息孤岛 | 协作效率 | 跨部门会议+全局EventBus+CEO综合简报 |
| CEO上下文溢出（管理太多部门Lead） | CEO效率 | CEO只与Lead交互，不直接管理Worker |
| CC单team agent数量过多 | 性能/协调 | CC层只放Lead，Worker通过subagent或轮转spawn |

---

## 10. 与现有OS能力的集成点

| 现有OS模块 | 部门架构的增强 |
|-----------|---------------|
| **team_create** | 增加department支持，创建团队时可指定部门结构 |
| **agent_register** | 增加department_id参数 |
| **taskwall_view** | 支持department_id筛选 |
| **meeting_create** | 支持department scope |
| **loop_engine** | 部门级循环（各部门独立循环节奏） |
| **event_bus** | 部门级事件channel |
| **team_briefing** | 增加部门维度聚合 |
| **hook_translator** | 根据agent的department_id路由事件 |

---

## 11. 结论

1. **CC平台的硬限制**（一个session一个team、不支持嵌套team、不支持跨team通信）使得真正的多层级Agent管理无法在CC内原生实现。

2. **方案B（OS增强方案）是最佳平衡点**：在CC的扁平team之上，通过OS层的数据模型、MCP工具、EventBus实现逻辑上的部门隔离和管理。实现代价可控（~2周），且不影响现有功能。

3. **方案A可作为快速验证**：仅需命名约定和prompt工程，1-2天即可验证部门概念是否对协作效率有实际提升。

4. **方案C（独立平台）是长期方向**：CC的限制是固有的，长期来看脱离CC直接调用LLM API是实现完整多部门架构的必经之路。但当前实施成本过高（3-6个月），建议作为长期战略储备。

5. **推荐策略**: A验证 → B实施 → C长期演进。先用方案A低成本验证部门概念的价值，确认有价值后再投入方案B的开发。

---

## 参考资料

- [Claude Code Agent Teams官方文档](https://code.claude.com/docs/en/agent-teams)
- [Claude Code Swarms分析](https://addyosmani.com/blog/claude-code-agent-teams/)
- [Agent Teams: From Tasks to Swarms](https://alexop.dev/posts/from-tasks-to-swarms-agent-teams-in-claude-code/)
- [Feature Request: Shared Channel for Agent Teams (#30140)](https://github.com/anthropics/claude-code/issues/30140)
- [CrewAI Flows + Crews多层编排](https://docs.crewai.com/en/concepts/tasks)
- [AutoGen Group Chat Design Patterns](https://microsoft.github.io/autogen/stable//user-guide/core-user-guide/design-patterns/group-chat.html)
- [Multi-Agent Framework Comparison 2026](https://www.datacamp.com/tutorial/crewai-vs-langgraph-vs-autogen)
- [Why CrewAI's Manager-Worker Architecture Fails](https://towardsdatascience.com/why-crewais-manager-worker-architecture-fails-and-how-to-fix-it/)
