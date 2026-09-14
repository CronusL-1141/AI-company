# meeting-facilitate 端到端示例

三份完整调用样例，按需读。调用骨架见 SKILL.md 的 Step 2 / Step 3；
这里只多给出三种模板下 `participants` 的 role 与 expected_output 该怎么写。

### 示例 1：架构方案 Council 评审

场景：评审 v0.9 Prompt Registry 设计文档，需要 arch-lead + backend-arch + ai-arch 三方评估。

```
# Step 1: 选模板 — council（多视角专家评审）

# Step 2: 创建会议
result = meeting_create(
    topic="Council 评审：v0.9 Prompt Registry 架构",
    template="council",
    team_id="repo-insight-arch",
    team_name="repo-insight-arch",
    materials=["docs/v0.9-prompt-registry.md"],
    participants=[
        {"name": "arch-lead", "agent_template": "software-architect",
         "role": "评估整体分层与可演进性",
         "context_files": ["docs/architecture.md"],
         "expected_output": "三段：分层评估 / 演进风险 / 评分 1-5"},
        {"name": "backend-arch", "agent_template": "backend-architect",
         "role": "评估存储与 API 契约",
         "context_files": ["src/aiteam/storage/repository.py"],
         "expected_output": "存储方案 / 接口契约 / 评分 1-5"},
        {"name": "ai-arch", "agent_template": "ai-engineer",
         "role": "评估 prompt 版本化对模型行为的影响",
         "context_files": [],
         "expected_output": "效果保留性 / 回滚策略 / 评分 1-5"},
    ],
)
meeting_id = result["data"]["id"]

# Step 3: Spawn 三位参与者（关键！）
for item in result["dispatch_plan"]:
    Agent(**item["launch_call"]["params"])

# Step 4: 等待 + 签到
status = meeting_attendance_check(meeting_id=meeting_id)
# pending=[] 后继续

# Step 5: 主持人引导（可选）
meeting_send_message(
    meeting_id=meeting_id, agent_id="team-lead", agent_name="team-lead",
    caller_agent_id="team-lead", round_number=1,
    content="【主持】Round 1 已收齐三方评估，进入 Round 2 交叉质询。",
)

# Step 6: 推进 Round 2、Round 3...

# Step 7: 结束
meeting_conclude(
    meeting_id=meeting_id,
    summary="三方一致 APPROVE，条件：backend-arch 提出的存储双写迁移方案需补 ADR；ai-arch 要求灰度验证回滚策略。",
)
```

### 示例 2：决策辩论（debate 模板）

场景：要决定 BM25 用 Tantivy 还是 Whoosh，存在分歧。

```
result = meeting_create(
    topic="决策辩论：BM25 选 Tantivy 还是 Whoosh",
    template="debate",
    participants=[
        {"name": "perf-advocate", "agent_template": "backend-architect",
         "role": "正方：主张 Tantivy（性能优先）",
         "context_files": ["benchmarks/bm25_compare.md"],
         "expected_output": "方案 + 数据 + 收益 + 局限"},
        {"name": "simple-critic", "agent_template": "code-reviewer",
         "role": "反方：质疑 Tantivy 引入 Rust 依赖的复杂度",
         "context_files": ["benchmarks/bm25_compare.md"],
         "expected_output": "引用正方原话 + 风险等级 + 替代方案"},
        {"name": "team-lead", "agent_template": "team-lead",
         "role": "裁决方",
         "context_files": [],
         "expected_output": "采纳点 / 最终结论 / Action Items"},
    ],
)
for item in result["dispatch_plan"]:
    Agent(**item["launch_call"]["params"])
# ... 后续 Steps 4-7
```

### 示例 3：Sprint 复盘（retrospective 模板）

场景：M6 阶段结束，全队 retrospective。

```
result = meeting_create(
    topic="M6 复盘：报告系统 DB 重构与 Dashboard 隔离",
    template="retrospective",
    materials=["docs/m6-summary.md"],
    participants=[
        {"name": "backend-dev", "agent_template": "backend-architect",
         "role": "后端开发视角", "context_files": [], "expected_output": "4Ls 各 1 条"},
        {"name": "frontend-dev", "agent_template": "frontend-developer",
         "role": "前端开发视角", "context_files": [], "expected_output": "4Ls 各 1 条"},
        {"name": "qa", "agent_template": "qa-engineer",
         "role": "测试视角", "context_files": [], "expected_output": "4Ls 各 1 条"},
    ],
)
for item in result["dispatch_plan"]:
    Agent(**item["launch_call"]["params"])
# ... Step 4 签到 → Step 6 推进到 Round 2 改进方向 → Round 3 承诺计划 → Step 7 conclude
```
