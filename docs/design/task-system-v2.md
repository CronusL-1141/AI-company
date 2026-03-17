# AI Team OS 任务系统 v2 设计文档

**状态**: 设计中 — 待用户审批
**作者**: Leader
**日期**: 2026-03-17
**关联问题**: 任务管理缺失、Leader认知过载、工作流不规范、规则注入重复

---

## 一、现状问题

### 1.1 任务数据模型不合理

**现状**: `POST /api/teams/{team_id}/tasks/run` — 任务绑定到团队
**问题**:
- 团队是临时执行单元（创建→工作→关闭），任务是持久项目目标
- 团队关闭后任务"消失"（不在active团队下）
- 同一任务不能跨团队接力
- 项目任务墙聚合依赖团队的project_id，新自动创建的团队可能缺失关联

### 1.2 Leader手动操作过多

13项手动操作中，高频遗忘：
- 创建任务到任务墙（用户提需求后直接开始干）
- 更新任务状态（agent完成后忙着做下一个）
- 添加任务memo（执行中很少想起记录）

### 1.3 规则注入重复且有缺口

- CLAUDE.md和session_bootstrap内容高度重复
- 两者都是session开头一次性注入，中后期淡化
- send_event.py只有3个检查，缺少任务管理相关提醒

### 1.4 工作流不规范

系统级功能直接跳过研究→设计→规划步骤，直接让工程师实施。

---

## 二、设计方案

### 2.1 任务模型重构

#### 2.1.1 任务属于项目，不属于团队

```
当前: Team → has many → Tasks
改为: Project → has many → Tasks
      Task → assigned_to → Team(可选) / Agent(可选)
```

**API变更**:
```
新增: POST /api/projects/{project_id}/tasks     — 项目级创建任务
保留: POST /api/teams/{team_id}/tasks/run        — 兼容旧接口，内部设project_id
新增: PUT  /api/tasks/{task_id}/assign           — 分配任务给团队/agent
修改: GET  /api/projects/{project_id}/task-wall   — 直接按project聚合（不依赖team）
```

**数据模型变更**:
```python
class Task:
    project_id: str          # 必填 — 任务属于哪个项目
    team_id: str | None      # 可选 — 当前由哪个团队执行
    assigned_to: str | None  # 可选 — 当前分配给哪个agent
    # 其他字段不变
```

#### 2.1.2 任务生命周期

```
pending → in_progress → completed
                      → blocked
                      → failed
```

状态变更触发点：
- `pending → in_progress`: Leader分配给agent时自动
- `in_progress → completed`: Leader手动标记（有提醒辅助）
- `in_progress → blocked`: 依赖未满足时

#### 2.1.3 MCP工具变更

```python
# 新增
task_create(project_id, title, description, priority, horizon)  # 项目级创建
task_assign(task_id, team_id=None, agent_name=None)             # 分配

# 保留
task_run        # 兼容，内部转为task_create + assign
task_memo_add   # 不变
task_memo_read  # 不变
taskwall_view   # 改为项目级聚合
```

### 2.2 工作流提醒系统

#### 2.2.1 设计原则

- 提醒挂载到**已有工具调用**的时间点，不需要AI语义分析
- 提醒是**建议性的**（stdout输出），不阻塞操作
- 频率控制：同一类提醒在N分钟内不重复
- 提醒内容必须**具体可操作**（告诉Leader该做什么，不是泛泛提醒）

#### 2.2.2 提醒矩阵

| 触发工具调用 | 触发时机 | 提醒内容 | 冷却时间 |
|-------------|---------|---------|---------|
| TeamCreate | PostToolUse | "[OS] 此工作方向是否已创建项目任务？→ task_create" | 无（每次TeamCreate都提醒） |
| Agent(team_name) | PreToolUse | "[OS] 此任务是否有历史memo？→ task_memo_read({推断的task_id})" | 5分钟 |
| 收到teammate completion消息 | PostToolUse(SendMessage) | "[OS] Agent完成工作。建议：①task_memo_add记录 ②更新任务状态" | 无 |
| SendMessage(shutdown) | PreToolUse | "[OS] 此agent任务是否已完成？建议：①标记完成 ②添加总结memo" | 无 |
| 任意工具调用 | PreToolUse | "[OS] 距上次查看任务墙已{N}分钟" | 15分钟 |
| PreCompact | Hook | "[OS] Compact前请：①更新进行中任务memo ②保存memory" | 无 |

#### 2.2.3 实现位置

全部在 `plugin/hooks/send_event.py` 的现有supervisor框架中扩展：

```python
# supervisor-state.json 扩展
{
    "leader_consecutive_calls": 0,      # 已有
    "team_create_pending_members": 0,   # 已有
    "last_taskwall_view": 1773600000,   # 新增
    "last_memo_reminder": 1773600000,   # 新增：防重复提醒
}
```

#### 2.2.4 检测teammate completion消息

**难点**: 收到teammate消息不经过工具调用hook。
**方案**: 不在hook层检测。改为在Agent的**标准化prompt模板**中引导Agent输出结构化汇报，Leader看到结构化汇报后自然想到更新状态。

### 2.3 规则注入体系优化

#### 2.3.1 三层分工

| 层级 | 载体 | 内容 | 特点 |
|------|------|------|------|
| **L1 持久层** | CLAUDE.md | 项目核心约束（技术栈、代码风格、3-5条最关键规则） | compact后仍在，精简不超过20行 |
| **L2 启动层** | session_bootstrap.py | 完整规则集+动态状态（任务墙+团队+自检清单） | 每次session启动注入 |
| **L3 持续层** | send_event.py | 上下文相关的工作流提醒 | 每次工具调用时，根据当前操作给出针对性提醒 |

#### 2.3.2 CLAUDE.md精简方案

```markdown
# AI Team OS

**技术栈**: Python 3.12 + FastAPI | React 19 + Vite | SQLite
**规则**: 完整规则通过SessionStart自动注入，也可查询 GET /api/system/rules

## 核心约束
- 所有输出使用中文
- 共享类型只引用 src/aiteam/types.py
- 代码风格: PEP 8，类型注解，async优先

## Leader核心行为（详细规则通过SessionStart注入）
- 专注统筹，实施工作委派团队成员
- 任务管理：新需求加入任务墙，完成后更新状态和memo
- 系统级功能：必须先设计文档再实施
```

移除`<!-- AI-TEAM-OS-RULES-START/END -->`段。

#### 2.3.3 bootstrap增强

在现有16条规则基础上增加：
- 当前进行中任务列表（不只是任务墙top5，还包括status=in_progress的）
- Leader自检提醒："新需求是否加入任务墙？进行中任务是否需要更新？"

### 2.4 Agent标准化prompt模板

#### 2.4.1 模板结构

```python
AGENT_PROMPT_TEMPLATE = """
你是AI Team OS的{role}。

## 你的任务
{task_description}

## 工作规范
1. 开始前：检查是否有task_memo记录前置工作（task_memo_read）
2. 执行中：关键进展和决策记录到task_memo
3. 完成后：向Leader汇报时使用以下格式

## 汇报格式
完成报告：
- 完成内容：{具体描述}
- 修改文件：{文件列表}
- 测试结果：{通过数/失败数}
- 建议任务状态：→completed / →blocked(原因)
- 建议memo内容：{一句话总结}

## 项目位置
{project_path}
"""
```

#### 2.4.2 Leader创建Agent时自动应用

在MCP的`agent_register` tool中，如果system_prompt为空，自动填充模板。
或者在`_on_subagent_start`自动注册时，用agent_name推断role并填充基础模板。

### 2.5 系统级功能工作流

#### 2.5.1 功能分级

| 级别 | 定义 | 工作流 |
|------|------|--------|
| **Quick Fix** | 改一行配置、修一个明确bug | 直接实施 |
| **Feature** | 新增API/页面/组件 | 简要设计→实施→测试 |
| **System** | 涉及架构/数据模型/多模块联动 | 研究→设计文档→用户审批→规划→实施→测试 |

#### 2.5.2 判断标准（写入B规则）

涉及以下任意一项 = System级：
- 修改数据模型（types.py / models.py）
- 修改3个以上模块
- 涉及hook/MCP/前端联动
- 新增系统级概念（如三状态模型）

---

## 三、实施计划

### Phase 1：工作流提醒（最快见效）
1. 扩展send_event.py的supervisor检查（6个新提醒）
2. bootstrap增加进行中任务列表
3. 预计改动：send_event.py ~50行 + bootstrap ~10行

### Phase 2：规则体系优化
1. CLAUDE.md精简
2. 移除AI-TEAM-OS-RULES段
3. bootstrap已有内容足够
4. 预计改动：CLAUDE.md重写 + bootstrap微调

### Phase 3：任务模型重构
1. 新增项目级task_create API
2. task_wall聚合改为按project_id直接查
3. 兼容旧task_run接口
4. 前端任务墙适配
5. 预计改动：routes/tasks.py + repository.py + MCP server.py + 前端

### Phase 4：Agent标准化模板
1. 定义AGENT_PROMPT_TEMPLATE
2. MCP agent_register自动填充
3. hook_translator auto-register使用模板

---

## 四、风险评估

| 风险 | 影响 | 缓解 |
|------|------|------|
| 任务模型重构影响现有数据 | 旧任务可能丢失team关联 | 迁移脚本保留team_id |
| 提醒太频繁变成噪音 | Leader忽略提醒 | 冷却时间+频率控制 |
| CLAUDE.md精简后compact丢规则 | 中后期规则淡化 | send_event.py持续层补充 |
| Agent模板过于死板 | 限制Agent灵活性 | 模板只是默认值，Leader可覆盖 |

---

## 五、待确认问题

1. 任务模型重构是否保留team_id字段（兼容）还是完全移除？
2. 工作流提醒的冷却时间（15分钟合理吗？）
3. Agent汇报模板是否应该强制还是建议？
4. CLAUDE.md精简后是否还需要保留规则段作为compact后的兜底？
