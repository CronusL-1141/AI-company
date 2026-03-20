# AI Team OS 战略行动计划

> 来源：战略决策会议 1e2232f1（2026-03-20）
> 参与者：tech-strategist、biz-strategist、execution-planner
> 输出人：execution-planner（务实执行顾问）

---

## 一、资源约束前提

**当前团队：1人（用户/董事长）+ 1 AI Leader**

这是所有决策的底层约束。同时只能有效推进 **1条主线 + 1条轻量辅线**。超过2条并行等于全部浅尝辄止。本计划严格遵守此约束。

---

## 二、战略决策表（最终共识）

| 事项 | 判定 | 三方共识度 | 前置条件 |
|------|------|-----------|----------|
| CC社区Skill整合（Superpowers + Planning规则） | **现在做** | 三方一致 | 无 |
| 多部门架构方案A（命名约定验证实验） | **现在做（1-2天小实验）** | 三方一致 | 无 |
| Partner Network申请 | **现在做（本周）** | 三方一致 | 产品基础Demo素材 |
| 开源核心框架准备 | **现在做（2周内发布）** | 三方一致 | 代码审计无敏感信息 |
| 多部门架构方案B（OS增强，5-6个MCP工具+DB迁移） | **Week 2-3条件启动** | 三方接受，时机有分歧 | 方案A验证结论（go/no-go门控） |
| MCP Skills标准封装 | **Week 3** | 三方一致 | 开源发布前完成 |
| Claude Agent SDK评估 | **Week 4（仅研究，不实现）** | 三方一致 | 无 |
| 独立Agent OS Phase 2（双模式） | **长期（3-6月）** | 三方一致 | SDK评估完成 + 成本基线建立 |
| 独立Agent OS Phase 3+（完整独立） | **长期（6-12月）** | 三方一致 | Phase 2验证通过 |
| 开源+Enterprise SaaS路径B | **长期** | 三方一致 | 融资或团队扩充后 |
| Anthropic战略合作路径C | **伺机而动** | 三方一致 | Partner Network通过后评估 |

---

## 三、4周行动计划

### Week 1：验证 + 规范 + 商业基础（零开发成本为主）

**主线：验证与整合**

- **Day 1-2：方案A快速验证实验**
  - 在现有CC team中用命名约定区分部门：`qa-lead`、`eng-lead`等
  - 通过CLAUDE.md/Agent Prompt注入部门身份：「你属于QA部门，直属上级是qa-lead」
  - 验收标准（必须在Day 1前定义）：**部门Lead是否有效减少team-lead直接介入次数？** 设定目标：观察到≥3次部门内自主协调
  - 输出：方案A验证报告（go/no-go结论）

- **Day 3-5：Agent规范升级**
  - 将Superpowers的TDD/systematic-debugging/code-review规则写入Agent System Prompt模板
  - 将Planning with Files的关键规则写入OS Agent规范：
    - 2-Action规则：每执行2次工具操作必须将关键发现持久化到task_memo
    - 3次失败升级协议：同一操作连续失败3次后自动上报Leader
    - 安全边界：区分可写入外部内容的文件和受保护的系统文件
  - 更新CLAUDE.md / Agent模板文件

- **并行（不占开发时间）：商业基础**
  - 填写Claude Partner Network申请表
  - 准备1-2张Dashboard截图作为Demo素材
  - 开始代码审计（Day 5，识别硬编码路径、环境配置、API key扫描）

**Week 1交付物：**
- 方案A验证报告（go/no-go）
- 更新的Agent执行规范（含Superpowers规则+2-Action+3次失败协议）
- Partner Network申请提交
- 代码审计初步报告

---

### Week 2：开源准备（完整一周，不分心）

**主线：开源发布前置工作**

- 代码清理：移除硬编码路径、环境特定配置、确认无API key/secret泄露
- 编写安装脚本，确保他人可独立安装运行
- README（英文主 + 中文副）：定位为「AI Team Operating System for Claude Code」
- 添加MIT LICENSE
- 快速入门文档：10分钟上手指南
- GitHub repo准备（建议使用GitHub Organization而非个人repo，提升品牌感）
- 准备3分钟产品演示材料（GIF/截图序列）

**条件触发：方案B启动**

- 如果Week 1方案A验证结论为**有效（go）**：
  - Week 2后半段（Day 3-5）开始Department数据模型设计+DB迁移方案
  - 不超出Week 2，DB迁移需向后兼容
- 如果方案A验证结论为**无效/待定（no-go）**：
  - Week 2全力开源准备，方案B推至Week 5+重新评估

**Week 2交付物：**
- 可公开发布的GitHub repo（README/LICENSE/文档/安装脚本）
- 方案A有效时：Department模型设计文档 + DB迁移脚本草案

---

### Week 3：开源发布 + 方案B核心（根据Week 2结论）

**主线：开源正式发布**

- GitHub公开发布（不追求完美，MVP状态即可）
- 同步提交：Product Hunt、Hacker News（Show HN: AI Team OS）
- 建立Discord社区频道（或GitHub Discussions）

**条件线：方案B核心实现（仅当方案A有效）**

- 实现3个核心MCP工具：
  - `department_create(team_id, name, lead_agent_id)`
  - `department_list(team_id)`
  - `department_assign_agent(department_id, agent_id, role)`
- TaskWall增加`department_id`筛选参数
- 完成DB迁移+数据模型

**并行：MCP Skills封装**

- 将现有28个MCP工具整理为标准MCP Skills格式
- 重点：完善错误处理、标准化返回格式、安全审计（Marketplace上架必要条件）

**Week 3交付物：**
- GitHub repo公开 + 发布宣传
- 方案B有效时：Department CRUD功能可用
- MCP Skills包初版

---

### Week 4：发布生态 + 研究启动

**主线：生态卡位**

- 向Claude Marketplace提交Plugin上架申请
- 收集早期用户反馈（GitHub Issues/Discord）
- 方案B补充（如Week 3完成核心）：
  - `department_briefing(department_id)`
  - EventBus部门级事件过滤
  - Meeting支持department scope

**研究线：未来路径准备**

- MCP tools提取为纯Python函数（解耦第一步）：
  - 将`src/aiteam/mcp/server.py`中的28个工具提取到`src/aiteam/tools/`纯函数模块
  - MCP Server变为thin adapter，调用同一套函数
  - 这是独立OS Phase 2的技术前置，不影响当前CC模式运行
- Claude Agent SDK评估（**仅文档研究+PoC设计，不进入实现**）：
  - 评估SDK对自定义工具支持、并发控制、上下文管理的实际限制
  - 输出：SDK vs 自建 vs LangGraph 技术选型报告

**Week 4交付物：**
- Marketplace上架申请提交
- MCP tools纯函数模块（初版）
- Agent SDK技术选型报告
- 下一个4周方向决策文档（基于用户反馈+技术评估）

---

## 四、关键决策门控

| 节点 | 时间 | 决策内容 | 触发条件 |
|------|------|---------|---------|
| 方案A门控 | Week 1 Day 2末 | 方案B是否启动 | 验证报告：有效/无效/待定 |
| 方案B节奏门控 | Week 2末 | 方案B是否继续 | DB迁移是否完成，有无阻塞 |
| 开源质量门控 | Week 3初 | GitHub发布是否就绪 | README/安装脚本/代码清理是否完成 |
| 后续方向决策 | Week 4末 | 下一个4周主线选择 | 用户数据+SDK评估+Marketplace反馈 |

---

## 五、执行风险与缓解

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| 范围蔓延（每个任务都有「顺便做」的诱惑） | 高 | 全部任务拖期 | 本计划每周任务量已是上限，新需求进入Week 5+ backlog |
| 方案A验证标准不清 | 中 | 无法做Week 2决策 | Week 1 Day 1必须先定义验收标准，再开始实验 |
| Week 2-3并行度过高（开源+方案B） | 中 | 质量下降 | 开源准备优先，方案B出现阻塞时立即暂停，不压缩开源质量 |
| Anthropic自建团队协作功能 | 中高 | 核心价值被覆盖 | 速度为先：4周内完成开源发布+Partner申请，建立先发品牌认知 |
| 市场化动作（Partner申请/开源宣传）分散注意力 | 中 | 技术进度延误 | Partner申请仅填表（不占开发时间），开源发布集中在Week 3，不分散前两周 |
| 商业实体/GitHub Organization等非技术前置缺失 | 中 | 阻塞Partner申请和Marketplace上架 | Week 1并行推进（不占开发时间，由用户/董事长处理） |

---

## 六、长期路线图（4周后）

```
Month 2-3（Week 5-12）:
  ├── 基于用户反馈迭代产品
  ├── Dashboard部门视图（方案B的前端部分）
  ├── 方案B剩余工具（若Week 3-4未完成）
  ├── MCP Skills Marketplace正式上线
  └── 独立OS Phase 2启动（双模式MVP）
      前提：Agent SDK评估完成 + 成本基线建立

Month 4-6（Week 13-24）:
  ├── 开源社区增长（目标：5000+ stars）
  ├── Enterprise版功能设计（监控/安全/权限）
  ├── 独立OS Phase 2验证（单Agent独立运行）
  └── 根据PMF决定：推进Phase 3 or 维持CC增强模式

Month 6+（长期）:
  ├── 独立OS Phase 3（多Agent并发，脱离CC）
  ├── Enterprise SaaS路径（需团队扩充）
  └── Anthropic战略合作/融资（基于社区规模和商业验证）
```

---

## 七、非技术前置清单（由用户/董事长推进）

以下事项不需要技术团队，但会阻塞商业化动作，建议Week 1并行推进：

1. **GitHub Organization**：创建正式组织账号（品牌感 > 个人repo）
2. **商业实体注册**：Partner Network申请和Marketplace上架的硬性要求
3. **品牌资产**：项目Logo、一句话value proposition（「AI Team OS: The Operating System for AI Agent Teams」）
4. **Partner Network申请**：[anthropic.com/partner](https://www.anthropic.com/partner) 填写申请表

---

## 八、本文档使用说明

- 本计划为4周滚动计划，Week 4末进行复盘后更新
- 每周开始时确认当周任务优先级，发现阻塞立即调整（不等到周末）
- 新需求统一进入「Week 5+ Backlog」，不插队进入当前4周

---

*文档生成时间：2026-03-20*
*会议ID：1e2232f1-24f3-43f7-a27f-402796dccded*
*综合人：execution-planner（务实执行顾问）*
