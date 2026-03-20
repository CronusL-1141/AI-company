# AI Team OS 系统完整性审查报告

> 审查日期: 2026-03-20
> 审查目标: 找出所有"散装"组件，设计紧密集成修复方案

---

## 1. Skills集成度审查

### 1.1 os-register (plugin/skills/os-register/SKILL.md)

| 维度 | 状态 | 说明 |
|------|------|------|
| CC自动调用 | ⚠️ 部分 | frontmatter标记`autoTrigger: true`，但CC的skill自动触发依赖CC自身机制，实际上Agent启动时不一定会自动执行此skill |
| OS规则引导 | ✅ 已有 | session_bootstrap.py在SessionStart注入规则，B0.4/B0.10等规则引导注册行为 |
| 与OS集成度 | ⚠️ 中等 | hook_translator.py已实现自动注册兜底（SubagentStart事件），但skill本身的手动流程（健康检查→注册→状态更新）与hook自动注册存在**双轨冗余** |

**断点**: Skill中步骤0.5调用已废弃的sync_rules.py（文件头标注DEPRECATED），应清理或更新引用。

### 1.2 meeting-facilitate (plugin/skills/meeting-facilitate/SKILL.md)

| 维度 | 状态 | 说明 |
|------|------|------|
| CC自动调用 | ❌ 无 | 无autoTrigger标记，完全手动 |
| OS规则引导 | ⚠️ 弱 | B6规则仅说"Round 1提出观点，Round 2+引用"，但没有"**何时应该开会**"的触发规则 |
| 与OS集成度 | ❌ 低 | OS有meeting_create等MCP工具，但无任何规则或提醒引导Leader在合适时机使用此skill |

**断点**: Leader不知道什么时候该主持会议。B0.1说"瓶颈时组织讨论会议"，但workflow_reminder.py没有检测"瓶颈"的逻辑。

### 1.3 meeting-participate (plugin/skills/meeting-participate/SKILL.md)

| 维度 | 状态 | 说明 |
|------|------|------|
| CC自动调用 | ❌ 无 | 无autoTrigger标记，完全手动 |
| OS规则引导 | ❌ 无 | 没有规则或提醒告诉Agent"你被邀请参加会议了，请使用meeting-participate skill" |
| 与OS集成度 | ❌ 低 | meeting_create返回参与者列表，但参与者收不到任何通知 |

**断点**: 会议创建后参与者无自动通知。当前依赖Leader手动用SendMessage通知每个Agent，流程极易断裂。

### 1.4 continuous-mode (plugin/skills/continuous-mode/SKILL.md)

| 维度 | 状态 | 说明 |
|------|------|------|
| CC自动调用 | ❌ 无 | 无autoTrigger标记，需用户手动触发 |
| OS规则引导 | ⚠️ 弱 | 规则中没有提及此skill，但loop_start等MCP工具已存在 |
| 与OS集成度 | ⚠️ 中等 | MCP工具（loop_start/loop_next_task/loop_review等）完整，但skill与规则系统脱节 |

**断点**: session_bootstrap.py不会提示"可使用/continuous-mode进入持续模式"，Leader不知道此能力存在。

---

## 2. Agent模板集成度审查

### 2.1 模板清单

`C:/Users/TUF/.claude/agents/` 下共 **26个模板**，按类别：

| 类别 | 模板 |
|------|------|
| **Engineering (13)** | ai-engineer, backend-architect, code-reviewer, database-optimizer, devops-automator, frontend-developer, git-workflow-master, mcp-builder, mobile-developer, rapid-prototyper, security-engineer, software-architect, sre |
| **Testing (4)** | api-tester, bug-fixer, performance-benchmarker, qa-engineer |
| **Management (2)** | project-manager, tech-lead |
| **Support (2)** | meeting-facilitator, technical-writer |
| **Specialized (1)** | workflow-architect |
| **Legacy (4)** | python-reviewer, refactor-cleaner, security-reviewer, tdd-guide |

### 2.2 OS感知度分析

| 维度 | 状态 | 说明 |
|------|------|------|
| agent_register知道模板？ | ❌ 不知道 | `agents.py`的`add_agent`接收name/role/system_prompt，完全不感知`.claude/agents/`下的模板 |
| 创建时推荐模板？ | ❌ 无推荐 | hook_translator.py使用`agent-prompt-template.md`（通用模板），不会根据角色推荐专用模板 |
| team_setup_guide引用？ | ❓ 未确认 | 需检查此MCP工具是否引用模板 |
| 模板与OS规则一致？ | ⚠️ 部分 | 模板中有自己的工作规范（如qa-engineer的"有罪推定"），但不引用OS的B类规则 |

### 2.3 核心断点

1. **OS不知道模板存在**: agent-prompt-template.md是通用的简单模板（仅含角色名+汇报格式），而`.claude/agents/`下有26个高质量专业模板，**完全未被利用**
2. **Leader无法浏览可用角色**: 没有API或MCP工具可以列出可用模板
3. **模板缺少OS集成段**: 模板不包含"注册到OS"、"遵守OS规则"、"使用task_memo"等指引

---

## 3. 规则体系完整性审查

### 3.1 现有规则概览

- **A类（自动执行）**: 18条 (A1-A18)，覆盖agent生命周期、session管理、冲突检测、活动追踪、事件系统
- **B类（建议性）**: 21条 (B0-B9)，覆盖leadership、coordination、memory、context、meeting、output、testing

### 3.2 现有Hook提醒

workflow_reminder.py实现了以下检查：
- TeamCreate后提醒任务上墙
- Agent创建前提醒检查历史memo
- SendMessage(shutdown)前提醒确认任务完成
- TeamDelete时关闭OS团队
- TeamCreate时检查多团队冲突
- SendMessage后检查空闲Agent+pending任务匹配
- 15分钟未查看任务墙提醒
- Agent汇报完成时提醒分配后续任务
- 安全护栏（危险命令、敏感信息、敏感文件）
- Leader连续工具调用过多提醒（B0.9）
- 团队缺少常驻成员提醒（B0.10）
- Agent创建未使用team_name提醒（B0.4）

### 3.3 缺失的规则/提醒

| 编号 | 缺失项 | 严重度 | 说明 |
|------|--------|--------|------|
| G1 | **会议创建时机提醒** | 高 | B0.1说"瓶颈时开会"，但没有自动检测瓶颈的机制。当所有任务完成或blocked超过N分钟时应提醒开会 |
| G2 | **会议参与者通知** | 高 | meeting_create后没有自动通知参与Agent的机制，完全依赖Leader手动SendMessage |
| G3 | **Skill使用时机提醒** | 高 | 没有任何规则或提醒引导使用meeting-facilitate/meeting-participate/continuous-mode等skill |
| G4 | **Agent模板推荐** | 中 | 创建Agent时无根据任务类型推荐合适模板的机制 |
| G5 | **"说了要做的事必须上墙"强制规则** | 高 | 对话中讨论的待办事项没有自动检测和提醒上墙的机制 |
| G6 | **任务完成验收流程** | 中 | 任务标记完成后没有自动触发QA验收的规则。B0.8说"新功能同步QA"但无自动提醒 |
| G7 | **Agent汇报标准化执行** | 中 | B0.13定义了汇报模板，agent-prompt-template.md也有格式，但hook不检查汇报是否符合格式 |
| G8 | **经验沉淀规则** | 低 | 项目/阶段完成后没有自动触发回顾和经验记录的规则 |
| G9 | **Skill存在性提醒** | 中 | session_bootstrap.py不输出"可用Skills列表"，Leader不知道有哪些skill可用 |
| G10 | **模板存在性通知** | 低 | session_bootstrap.py不输出"可用Agent模板"信息 |

---

## 4. 组件协作流审查

```
用户下达需求
  │
  ▼
Leader创建团队(TeamCreate)                    ⚠️ 部分集成
  │ ├─ workflow_reminder提醒任务上墙           ✅
  │ ├─ workflow_reminder检查多团队冲突         ✅
  │ └─ 提醒创建常驻成员                       ✅ (每20次工具调用检查)
  │ ✗ 无"推荐使用continuous-mode"提醒          ❌
  │
  ▼
创建任务(task_create)                          ⚠️ 部分集成
  │ ├─ 任务墙可见                             ✅
  │ ├─ 任务依赖自动阻塞(A17)                  ✅
  │ └─ 任务优先级排序                         ✅
  │ ✗ 对话中口头承诺的事项无自动检测上墙       ❌ (G5)
  │
  ▼
创建Agent(CC Agent tool)                       ⚠️ 部分集成
  │ ├─ workflow_reminder检查team_name          ✅
  │ ├─ hook_translator自动注册到OS(A1/A2)      ✅
  │ ├─ workflow_reminder提醒检查历史memo        ✅
  │ └─ agent-prompt-template自动填充           ✅
  │ ✗ 不推荐专业模板(.claude/agents/)           ❌ (G4)
  │ ✗ 不知道26个高质量模板的存在               ❌
  │
  ▼
Agent注册(os-register skill)                   ⚠️ 冗余
  │ ├─ hook已自动注册，skill是手动备份流程      ⚠️ 双轨冗余
  │ └─ skill引用已废弃的sync_rules.py          ❌ 需清理
  │
  ▼
开会讨论(meeting_create)                       ❌ 断开严重
  │ ├─ MCP工具可用                             ✅
  │ ├─ B6有讨论规则                            ✅
  │ ✗ 无"何时该开会"的自动检测                  ❌ (G1)
  │ ✗ 参与者无自动通知                         ❌ (G2)
  │ ✗ 无提醒使用meeting-facilitate skill        ❌ (G3)
  │ ✗ 参与者不知道使用meeting-participate skill  ❌ (G3)
  │
  ▼
Agent执行任务                                  ✅ 基本集成
  │ ├─ 工具使用自动记录(A12)                   ✅
  │ ├─ 文件冲突检测(A10/A11)                   ✅
  │ ├─ 状态自愈(A6)                            ✅
  │ └─ last_active_at自动更新(A14)             ✅
  │
  ▼
Agent汇报完成                                  ⚠️ 部分集成
  │ ├─ workflow_reminder检测完成关键词并提醒    ✅
  │ ├─ B0.13定义汇报模板                       ✅
  │ ├─ agent-prompt-template包含汇报格式        ✅
  │ ✗ 不检查汇报是否符合标准格式               ❌ (G7)
  │ ✗ 不自动触发QA验收                         ❌ (G6)
  │
  ▼
任务验收                                       ❌ 断开
  │ ✗ 无自动QA介入机制                         ❌ (G6)
  │ ✗ 无互审流程                               ❌
  │ ✗ 任务完成后无自动通知QA                    ❌
  │
  ▼
项目完成                                       ❌ 断开
  │ ├─ SessionEnd自动关闭团队                   ✅
  │ ✗ 无自动汇报/经验沉淀机制                  ❌ (G8)
  │ ✗ 无项目完成回顾提醒                        ❌
```

---

## 5. 修复方案

### 优先级P0 — 核心断点（必须修复）

#### F1: 会议系统集成 (修复G1+G2+G3)

**问题**: 会议系统有完整工具但无使用引导，是最严重的"散装"组件。

**方案A — 规则+提醒层（推荐）**:

1. **system.py新增规则**:
```python
{
    "id": "B6.1",
    "category": "meeting",
    "name": "会议触发时机",
    "description": "以下情况应组织会议：1)任务全部完成需讨论方向 2)多任务blocked需协调 3)技术方案需多角度评估 4)阶段性里程碑回顾",
    "advice": "使用meeting-facilitate skill主持会议。会议结论必须转化为任务墙上的具体任务",
},
{
    "id": "B6.2",
    "category": "meeting",
    "name": "会议参与者通知",
    "description": "meeting_create后必须通过SendMessage通知每位参与者，告知meeting_id和讨论主题",
    "advice": "通知模板：'会议通知：[topic]，ID=[meeting_id]，请使用meeting-participate skill参与讨论'",
},
```

2. **workflow_reminder.py新增检测**:
```python
# 新增: meeting_create后提醒通知参与者
if tool_name in ("meeting_create", "mcp__ai-team-os__meeting_create"):
    warnings.append(
        "[OS提醒] 会议已创建。请逐一通知参与者(SendMessage)，告知meeting_id和讨论规则。"
        "参与者应使用 /meeting-participate skill 参加讨论"
    )

# 新增: 检测所有任务完成/blocked时提醒开会
# (在SendMessage-completion检查附近添加)
```

3. **session_bootstrap.py新增Skill提示段**:
```python
lines.append("")
lines.append("=== 可用Skills ===")
lines.append("- /meeting-facilitate — 主持多Agent讨论会议")
lines.append("- /meeting-participate — 参与会议讨论")
lines.append("- /continuous-mode — 启动持续工作循环")
lines.append("- /os-register — 手动注册到OS（通常已自动完成）")
```

#### F2: "说了要做的事上墙"提醒 (修复G5)

**问题**: 讨论中的待办事项容易遗忘，不上任务墙。

**方案**:

1. **workflow_reminder.py新增检测**:
```python
# 新增: 会议结束后提醒将结论转任务
if tool_name in ("meeting_conclude", "mcp__ai-team-os__meeting_conclude"):
    warnings.append(
        "[OS提醒] 会议已结束。请将讨论结论中的行动项转化为任务墙上的具体任务。"
        "→ 使用 task_create 或 task_run 添加任务"
    )
```

2. **system.py新增规则**:
```python
{
    "id": "B0.14",
    "category": "leadership",
    "name": "行动项必须上墙",
    "description": "对话/会议中产生的所有行动项必须在任务墙创建对应任务，口头承诺不算",
    "advice": "每次讨论后检查：是否有新的待办事项？是否已在任务墙上？",
},
```

#### F3: 任务完成自动触发QA (修复G6)

**问题**: 任务标记完成后没有通知QA验收的机制。

**方案**:

**workflow_reminder.py新增检测**:
```python
# 新增: task_status设为completed时提醒QA验收
if tool_name in ("task_status", "mcp__ai-team-os__task_status"):
    input_str = str(event_data.get("tool_input", {}))
    if "completed" in input_str.lower() or "complete" in input_str.lower():
        warnings.append(
            "[OS提醒] 任务已标记完成。此任务是否涉及系统行为变更？"
            "→ 如是，请通知QA进行验收测试(B0.8)"
        )
```

---

### 优先级P1 — 重要改进

#### F4: Agent模板感知与推荐 (修复G4+G10)

**问题**: 26个高质量Agent模板完全未被OS利用。

**方案分三步**:

**步骤1 — 模板索引API** (新增路由):

在`src/aiteam/api/routes/`下新增`agent_templates.py`:
```python
# GET /api/agent-templates — 扫描~/.claude/agents/目录，返回模板列表
# GET /api/agent-templates/{name} — 返回单个模板内容
# GET /api/agent-templates/recommend?task_type=frontend — 根据任务类型推荐模板
```

模板推荐逻辑基于frontmatter的name/description与任务tags/title的关键词匹配。

**步骤2 — MCP工具封装**:

新增MCP tool `agent_template_list` 和 `agent_template_recommend`，供Leader在创建Agent前查询。

**步骤3 — workflow_reminder集成**:

```python
# Agent创建前提醒使用模板
if tool_name == "Agent":
    input_str = str(event_data.get("tool_input", {}))
    if "team_name" in input_str:
        warnings.append(
            "[OS提醒] 创建团队成员前：是否需要查看可用Agent模板？"
            "→ 使用 agent_template_recommend 获取角色推荐"
        )
```

#### F5: os-register Skill清理

**问题**: Skill中步骤0.5引用已废弃的sync_rules.py。

**方案**:
- 删除SKILL.md中步骤0.5（sync_rules.py调用）
- 在步骤0中改为说明"规则已通过SessionStart自动注入，可通过GET /api/system/rules查看完整规则"
- 明确说明此skill是手动备份流程，正常情况下hook_translator已自动完成注册

#### F6: session_bootstrap增强 — 输出可用能力

**方案**: 在session_bootstrap.py的`_build_briefing()`中追加：

```python
# 可用Skills段
lines.append("=== 可用Skills ===")
lines.append("- /meeting-facilitate — 需要组织多Agent讨论时使用")
lines.append("- /meeting-participate — 被邀请参加会议时使用")
lines.append("- /continuous-mode — 启动自动循环领取任务模式")
lines.append("")

# 可用Agent模板段（从~/.claude/agents/扫描）
lines.append("=== 可用Agent模板 ===")
lines.append("创建成员前可参考以下专业模板：")
# ... 扫描目录列出模板分类
```

---

### 优先级P2 — 锦上添花

#### F7: 会议瓶颈自动检测 (G1增强)

**方案**: workflow_reminder.py中，基于API查询任务状态：
- 当pending任务数=0且running任务数=0 → 提醒"任务清空，建议组织方向讨论会议"
- 当blocked任务数 > running任务数 → 提醒"多任务阻塞，建议组织协调会议"

#### F8: Agent汇报格式校验 (G7)

**方案**: workflow_reminder.py中，当检测到Agent完成汇报(SendMessage含完成关键词)时，检查消息内容是否包含汇报模板的关键字段（"完成内容"、"修改文件"、"测试结果"），缺少时提醒。

#### F9: 项目完成经验沉淀 (G8)

**方案**:
- system.py新增规则B0.15"项目/阶段完成时组织回顾会议，记录经验教训到memory"
- workflow_reminder.py检测到所有任务完成时提醒回顾

#### F10: 模板注入OS规则引用

**方案**: 在`.claude/agents/`的模板中统一添加OS集成段：
```markdown
## AI Team OS集成
- 启动后自动注册到OS（hook_translator处理，无需手动）
- 遵守系统规则（GET /api/system/rules）
- 有task_id时使用task_memo记录进度
- 完成后使用标准汇报格式向Leader汇报
```

---

## 6. 修复优先级总览

| 优先级 | 编号 | 修复项 | 涉及文件 | 工作量 |
|--------|------|--------|----------|--------|
| **P0** | F1 | 会议系统集成 | system.py + workflow_reminder.py + session_bootstrap.py | 中 |
| **P0** | F2 | 行动项上墙提醒 | workflow_reminder.py + system.py | 小 |
| **P0** | F3 | 任务完成触发QA | workflow_reminder.py | 小 |
| **P1** | F4 | Agent模板感知 | 新增agent_templates.py + MCP tool + workflow_reminder.py | 大 |
| **P1** | F5 | os-register清理 | plugin/skills/os-register/SKILL.md | 小 |
| **P1** | F6 | Bootstrap输出增强 | session_bootstrap.py | 小 |
| **P2** | F7 | 瓶颈自动检测 | workflow_reminder.py | 中 |
| **P2** | F8 | 汇报格式校验 | workflow_reminder.py | 小 |
| **P2** | F9 | 经验沉淀机制 | system.py + workflow_reminder.py | 小 |
| **P2** | F10 | 模板OS集成段 | .claude/agents/*.md (26个文件) | 中 |

---

## 7. 集成度评分

| 组件 | 当前评分 | 修复后预期 |
|------|----------|-----------|
| Agent生命周期 (注册/状态/冲突) | 9/10 | 9/10 |
| 任务管理 (创建/分配/追踪) | 8/10 | 9/10 |
| Hook提醒系统 | 7/10 | 9/10 |
| 规则体系 | 7/10 | 9/10 |
| **会议系统** | **3/10** | **8/10** |
| **Skills集成** | **2/10** | **7/10** |
| **Agent模板** | **1/10** | **7/10** |
| Session引导 | 7/10 | 9/10 |
| 安全护栏 | 8/10 | 8/10 |
| **整体系统紧密度** | **5.8/10** | **8.3/10** |

---

## 8. 核心结论

AI Team OS的**底层基础设施**（hook事件处理、agent生命周期、任务管理、冲突检测）已经非常紧密和完整。主要问题集中在**上层引导层**：

1. **会议系统是最大断点** — 有完整的MCP工具和Skills，但完全没有自动触发/引导机制，形同虚设
2. **26个Agent模板是最大浪费** — 高质量专业模板完全未被OS感知和利用
3. **Skills存在但隐形** — CC不会自动提醒使用，session_bootstrap也不列出可用Skills

修复策略的核心思路是**不修改底层架构，只增强引导层**：
- 在system.py中补充缺失规则（声明式）
- 在workflow_reminder.py中补充检测逻辑（运行时提醒）
- 在session_bootstrap.py中补充能力展示（启动时告知）
- 新增agent_templates路由（连接模板与OS）

这些修复都是增量性的，不影响现有功能，可以逐步实施。
