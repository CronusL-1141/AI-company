# AI Team OS -- 长期记忆系统技术调研报告

> 调研日期: 2026-03-14
> 调研目标: 为AI Agent团队操作系统设计支持语义搜索、多scope、自动归档衰减的长期记忆系统
> 基于现有技术栈: PostgreSQL + pgvector + Python + FastAPI + Mem0/SQLite双后端

---

## 目录

1. [现有系统分析](#1-现有系统分析)
2. [向量记忆库对比](#2-向量记忆库对比)
3. [Embedding模型选择](#3-embedding模型选择)
4. [记忆架构设计模式](#4-记忆架构设计模式)
5. [参考实现分析](#5-参考实现分析)
6. [推荐方案](#6-推荐方案)

---

## 1. 现有系统分析

### 1.1 当前架构

项目已实现三温度记忆管理 (`MemoryStore`):

| 层级 | 存储 | 特点 |
|------|------|------|
| Hot层 | Python dict内存缓存 | 按 `scope:scope_id` 索引，最快访问 |
| Warm层 | MemoryBackend (SQLite/Mem0) | 持久化，通过Protocol抽象 |
| Cold层 | JSON文件归档 | 离线存储，手动归档 |

### 1.2 现有不足

| 问题 | 详情 |
|------|------|
| 搜索方式 | 仅关键词匹配 (`_tokenize` 中英文分词 + 集合交集计数)，无语义理解 |
| 无向量化 | Memory模型不含embedding字段，MemoryModel表无vector列 |
| 归档被动 | 需手动调用 `archive()`，无自动衰减/过期机制 |
| 无重要性评分 | 所有记忆权重相同，无法区分关键记忆和琐碎记忆 |
| Mem0集成浅 | 仅包装SDK调用，未利用pgvector直连，额外依赖Mem0服务 |
| 无Hybrid Search | 关键词和语义搜索互相独立，未融合排序 |

### 1.3 可复用资产

- `MemoryBackend` Protocol -- 抽象层设计良好，新后端只需实现5个方法
- `ResilientMemoryBackend` -- Circuit Breaker降级机制可直接复用
- `MemoryScope` 枚举 -- global/team/agent/user四级已定义
- `ContextRecovery` -- 检查点机制可与记忆系统互补
- PostgreSQL + pgvector -- 基础设施已在docker-compose中配置就绪

---

## 2. 向量记忆库对比

### 2.1 综合对比表

| 维度 | pgvector | Mem0 | ChromaDB | Qdrant | FAISS | LanceDB | Milvus | Weaviate |
|------|----------|------|----------|--------|-------|---------|--------|----------|
| **类型** | PG扩展 | 记忆层框架 | 嵌入式向量DB | 独立向量DB | 搜索库 | 嵌入式向量DB | 分布式向量DB | 独立向量DB |
| **语言** | C | Python | Rust(v2) | Rust | C++ | Rust | Go/C++ | Go |
| **部署复杂度** | 低(已有PG) | 中(需3容器) | 低(嵌入式) | 中 | 低(库) | 低(嵌入式) | 高(分布式) | 中 |
| **与PG兼容** | 原生 | 支持pgvector后端 | 不兼容 | 不兼容 | 不兼容 | 不兼容 | 不兼容 | 不兼容 |
| **向量规模上限** | 10-100M | 取决于后端 | 10M | 100M+ | 10亿+ | 100M+ | 10亿+ | 100M+ |
| **Hybrid Search** | 原生FTS+向量 | 通过后端 | 元数据过滤 | 丰富过滤 | 无 | 元数据过滤 | 标量+向量 | 内置BM25 |
| **持久化** | 原生 | 原生 | 原生 | 原生 | 需自建 | 原生(Lance格式) | 原生 | 原生 |
| **GitHub Stars** | 14K+ | 41K+ | 18K+ | 23K+ | 33K+ | 5K+ | 35K+ | 12K+ |
| **生产就绪** | 高 | 高 | 中 | 高 | 高(库) | 中 | 高 | 高 |
| **成本** | 免费(已有PG) | 免费(OSS)/付费 | 免费 | 免费/付费 | 免费 | 免费 | 免费/付费 | 免费/付费 |
| **适用场景** | 已有PG的项目 | Agent记忆专用 | 快速原型 | 高性能过滤 | 研究/大规模 | 边缘/嵌入式 | 超大规模 | 多模态AI |

### 2.2 关键性能数据 (2025-2026基准测试)

| 指标 | pgvector(+pgvectorscale) | Qdrant | ChromaDB | FAISS |
|------|--------------------------|--------|----------|-------|
| QPS@99%recall (50M向量) | **471** | 41 | N/A | 高(GPU) |
| 并发100请求平均耗时 | **9秒** | 未公布 | 严重退化 | N/A |
| 与pgvectorscale对比 | 基准 | 慢11.4x | - | - |
| p95延迟 vs Pinecone | **低28x** | - | - | - |

> pgvector 0.8.0 (2025年5月) 对比0.7.4版本: 查询性能提升5.7x，搜索相关性提升100x

### 2.3 各库详细分析

#### pgvector -- 首选 (已就绪)

**优势:**
- 零额外基础设施 -- PostgreSQL已在项目docker-compose中
- 2026年已不再是"慢选项"，性能与Pinecone竞争
- 原生支持Hybrid Search: pgvector向量搜索 + tsvector全文搜索
- pg_textsearch扩展带来真正的BM25排序
- HNSW索引提供高recall率，适合生产RAG
- 事务一致性 -- 记忆的CRUD与业务数据在同一事务中
- SQLAlchemy 2.0 完整支持

**劣势:**
- 超过100M向量后性能下降（项目远不到此规模）
- 不支持GPU加速（项目不需要）

#### Mem0 -- 高级记忆层 (已集成)

**优势:**
- 专为AI Agent记忆设计，一行代码添加记忆
- 2025年融资$24M，社区活跃(41K+ stars，186M+ API调用/Q3 2025)
- 2026年推出Graph Memory -- 用Neo4j捕获实体关系
- 支持pgvector作为向量后端 -- 可与现有PG共用
- 91%更低p95延迟，90%更少token消耗
- SOC 2 & HIPAA合规
- CrewAI/Langflow/AWS Agent SDK原生集成

**劣势:**
- 自托管需3个容器(FastAPI + PG + Neo4j)
- Graph Memory增加Neo4j依赖
- 对embedding维度敏感（需配置 `embedding_model_dims`）

#### ChromaDB -- 不推荐

**原因:** 单请求快但并发性能严重退化；需引入新的存储基础设施；与PG不兼容。适合原型，不适合生产。

#### Qdrant -- 备选

**原因:** Rust实现高性能，但需部署独立服务；丰富的payload过滤能力出色。如果未来需要复杂元数据过滤可考虑。

#### FAISS -- 不推荐

**原因:** 纯搜索库无持久化层，需要大量集成工作。适合研究场景，不适合Agent记忆系统。

#### LanceDB -- 观望

**原因:** 嵌入式设计轻量，但生态不够成熟(5K stars)，社区较小。

#### Milvus -- 过度工程化

**原因:** 为超大规模设计(10亿+向量)，部署复杂(分布式架构)，项目记忆规模远不需要。

#### Weaviate -- 不推荐

**原因:** 内置向量化模块虽方便，但引入额外依赖和延迟；与现有PG基础设施不兼容。

### 2.4 选型结论

```
推荐: pgvector (核心) + Mem0 (高级特性，可选)
```

**理由:**
1. pgvector已就绪，零新增基础设施成本
2. 原生Hybrid Search (向量 + BM25全文)，2026年性能已追上专用向量DB
3. 与SQLAlchemy/Alembic生态无缝集成
4. Mem0可作为可选高级层，通过pgvector后端共用同一PG实例
5. 项目记忆规模预估 < 1M条，pgvector绰绰有余

---

## 3. Embedding模型选择

### 3.1 模型对比表

| 模型 | 维度 | 最大token | MTEB分数 | 中文支持 | 部署方式 | 价格 |
|------|------|-----------|----------|----------|----------|------|
| **OpenAI text-embedding-3-small** | 1536 | 8191 | 62.3 | 好 | API | $0.02/MTok |
| **OpenAI text-embedding-3-large** | 3072 | 8191 | 64.6 | 好 | API | $0.13/MTok |
| **Voyage voyage-3.5** | 1024 | 32000 | 高 | 好 | API | $0.06/MTok |
| **Voyage voyage-3-large** | 1024 | 32000 | SOTA | 好 | API | $0.18/MTok |
| **BGE-M3 (BAAI)** | 1024 | 8192 | 63.0 | **极好** | 本地/API | 免费 |
| **all-MiniLM-L6-v2** | 384 | 512 | 56.3 | 差 | 本地 | 免费 |
| **Qwen-3 Embedding** | 多种 | 8192 | 高 | **极好** | 本地/API | 免费/低价 |

### 3.2 关键发现

#### Anthropic不提供Embedding API

Anthropic官方不提供自有的embedding模型。其文档推荐使用合作伙伴 **Voyage AI** 的模型。

#### BGE-M3 -- 中文场景最佳开源选择

- 由北京智源研究院(BAAI)开发，中英双语表现顶尖
- 支持100+语言，8192 token长文本
- 三合一检索: 稠密检索 + 多向量检索 + 稀疏检索
- 可通过 `ollama pull bge-m3` 一键本地部署
- CMTEB(中文MTEB)基准表现优秀
- 完全免费，无API调用限制

#### 本地 vs API调用的Trade-off

| 维度 | 本地部署 (BGE-M3) | API调用 (OpenAI/Voyage) |
|------|-------------------|------------------------|
| 延迟 | 10-50ms (GPU) / 100-500ms (CPU) | 100-300ms (网络往返) |
| 成本 | 一次性(硬件) | 按用量持续付费 |
| 隐私 | 数据不出本地 | 数据发送到云端 |
| 可靠性 | 不依赖外部服务 | 受API限流/宕机影响 |
| 中文质量 | BGE-M3极优 | 好但非专攻中文 |
| 维护成本 | 需管理模型更新 | 零维护 |
| GPU需求 | 推荐(可CPU但慢) | 无需GPU |

### 3.3 Embedding选型建议

```
首选: BGE-M3 本地部署 (通过Ollama或sentence-transformers)
备选: OpenAI text-embedding-3-small (API方式，简单集成)
高级: Voyage voyage-3.5 (Anthropic推荐，性价比高)
```

**推荐理由:**
1. 项目使用中文为主 -- BGE-M3是目前中文embedding的SOTA级选择
2. 项目已有本地部署基础设施(Docker) -- 可在同一环境运行
3. 免费无限使用 -- 不增加运营成本
4. 1024维向量 -- 在质量和存储空间之间取得平衡
5. 三合一检索 -- 稠密+稀疏+多向量可配合Hybrid Search

**降级策略:** 如果本地GPU不可用，降级到OpenAI text-embedding-3-small（API调用，维度1536，价格极低）

---

## 4. 记忆架构设计模式

### 4.1 记忆层级结构

参照认知科学和MemGPT的设计，推荐三层记忆:

```
┌─────────────────────────────────────────────┐
│         工作记忆 (Working Memory)             │
│   当前会话的即时上下文 -- 在LLM context中       │
│   类比: CPU寄存器/L1缓存                       │
│   存储: 内存 (现有Hot层)                        │
│   保留: 会话结束后丢弃或降级                     │
├─────────────────────────────────────────────┤
│         情景记忆 (Episodic Memory)             │
│   具体事件和交互记录 -- 带时间戳的经历            │
│   类比: RAM                                    │
│   存储: PostgreSQL (Warm层)                     │
│   保留: 数周到数月，按衰减策略自动降级            │
│   示例: "2026-03-14 agent-A完成了任务X"          │
├─────────────────────────────────────────────┤
│         语义记忆 (Semantic Memory)             │
│   提炼后的知识和规则 -- 去除时间上下文的事实       │
│   类比: 硬盘                                   │
│   存储: PostgreSQL + 向量索引 (Cold层升级)       │
│   保留: 长期/永久，通过合并压缩                  │
│   示例: "Python项目偏好使用ruff做lint"           │
└─────────────────────────────────────────────┘
```

### 4.2 记忆衰减策略

#### 综合评分公式

```
Score(memory, t) = w_importance * importance
                 + w_recency   * recency(t)
                 + w_frequency * frequency
                 + w_relevance * relevance(query)
```

各因子定义:

| 因子 | 计算方式 | 推荐权重 |
|------|----------|----------|
| **importance** | LLM评分(1-10) 或 规则评分 | 0.3 |
| **recency** | `e^(-lambda * age_hours)`，lambda=0.005 (半衰期约6天) | 0.2 |
| **frequency** | `log(1 + access_count)` 归一化 | 0.2 |
| **relevance** | 向量余弦相似度 (0-1) | 0.3 |

#### 衰减时间表

| 记忆年龄 | 衰减行为 |
|----------|----------|
| 0-24小时 | 全保留，recency > 0.88 |
| 1-7天 | 轻微衰减，recency 0.88-0.71 |
| 7-30天 | 中度衰减，低importance记忆开始候选归档 |
| 30-90天 | 高度衰减，仅高importance记忆保留在Warm层 |
| 90天+ | 自动归档到Cold层(压缩JSON)，或合并为语义记忆 |

#### 自动维护任务

```python
# 每日运行的记忆维护计划
async def daily_memory_maintenance():
    # 1. 计算所有记忆的综合评分
    # 2. 评分 < threshold 且 age > 30天 → 归档到Cold层
    # 3. 相似度 > 0.95 的记忆对 → 合并(保留综合评分更高的)
    # 4. age > 90天 且 access_count == 0 → 候选删除(需确认)
    # 5. 提炼高频情景记忆 → 生成语义记忆
```

### 4.3 记忆合并/压缩

#### 去重策略

```
1. 向量相似度 > 0.95 → 标记为重复候选
2. LLM判断是否语义等价
3. 等价 → 保留综合评分更高的，合并metadata
4. 互补 → LLM生成合并摘要，创建新记忆
```

#### 情景 -> 语义 提炼

```
输入: 5条关于"项目偏好ruff"的情景记忆
  - "2026-01-15 使用ruff替代flake8进行lint"
  - "2026-02-01 ruff配置添加了I规则"
  - "2026-02-20 ruff版本升级到0.9"
  - "2026-03-01 新模块也用ruff"
  - "2026-03-10 ruff检查通过"

输出: 1条语义记忆
  - "本项目统一使用ruff (>=0.8) 做代码lint，配置在pyproject.toml中，包含E/F/I/N/W/UP规则"
```

### 4.4 RAG在Agent记忆中的应用

```
Agent收到任务
    │
    ├─1. 提取查询关键词 (LLM/规则)
    │
    ├─2. Hybrid Search
    │   ├─ 向量搜索: pgvector cosine similarity (语义匹配)
    │   ├─ 关键词搜索: tsvector + BM25 (精确匹配)
    │   └─ RRF融合: Reciprocal Rank Fusion 合并排序
    │
    ├─3. 多Scope聚合
    │   ├─ agent scope: 个人经验 (limit=5)
    │   ├─ team scope: 团队知识 (limit=3)
    │   └─ global scope: 全局规则 (limit=2)
    │
    ├─4. 重排序 (Reranking)
    │   └─ 综合评分 = relevance * 0.4 + importance * 0.3 + recency * 0.3
    │
    └─5. 注入Context
        └─ build_context_string(top_10_memories) → system prompt
```

---

## 5. 参考实现分析

### 5.1 对比表

| 框架 | 记忆架构 | 向量存储 | 特色 | 适用启示 |
|------|----------|----------|------|----------|
| **Letta/MemGPT** | 核心记忆+回忆记忆+档案记忆 | 可配置 | LLM自管理内存，OS范式 | 记忆分层思想值得借鉴 |
| **LangChain/LangGraph** | Checkpointer+RunnableHistory | 可配置 | 已弃用老Memory模块，转向LangGraph | 项目已用LangGraph，可利用Checkpointer |
| **CrewAI** | 短期+长期+实体+上下文 | ChromaDB+SQLite | 认知记忆，跨Flow累积 | 四种记忆类型划分清晰 |
| **AutoGPT** | 短期+长期(向量) | Pinecone/ChromaDB | 跨Session知识积累 | 简单实用的两层设计 |
| **Mem0** | 向量记忆+图记忆 | 20+后端 | 自动提取/合并/检索 | 可直接集成，pgvector后端 |

### 5.2 Letta (MemGPT) 深度分析

Letta的"LLM-as-OS"范式最为先进:

```
┌──────────────────────────────┐
│     Core Memory (RAM)         │  始终在context中，Agent可自行编辑
│  ├─ Human Block              │  用户信息
│  └─ Persona Block            │  Agent角色定义
├──────────────────────────────┤
│  Conversational Memory       │  可搜索的对话历史
│  (Recall Storage)            │  自动管理，按需检索
├──────────────────────────────┤
│  Archival Memory             │  长期知识存储
│  (Archival Storage)          │  无限容量，向量搜索
├──────────────────────────────┤
│  External Files              │  PDF/文档等外部知识
│  (Letta Filesystem)          │  2026年新增
└──────────────────────────────┘
```

**对我们项目的启示:**
- Agent应能"自编辑"核心记忆，而非仅被动存储
- Core Memory块可映射到我们的 `MemoryScope.AGENT` + metadata标记
- Archival Memory对应我们的Cold层升级版(加向量索引)

### 5.3 LangGraph Memory (项目已使用LangGraph)

LangGraph v0.3+ 推荐:
- `InMemorySaver` / `SqliteSaver` / `AsyncPostgresSaver` 做状态检查点
- `trim_messages()` 做对话历史裁剪
- 弃用 `ConversationBufferMemory` 等旧API

**对我们项目的启示:**
- 可利用LangGraph的 `AsyncPostgresSaver` 做状态持久化
- 与我们的MemoryStore互补: LangGraph管图执行状态，MemoryStore管语义记忆

### 5.4 CrewAI Memory

CrewAI四种记忆:
1. **Short-term**: ChromaDB + RAG，任务内共享
2. **Long-term**: SQLite，跨任务持久化
3. **Entity**: RAG识别关键实体
4. **Contextual**: 上下文摘要

**对我们项目的启示:**
- Entity Memory概念有价值 -- 自动识别和追踪项目中的关键实体
- 2026 Q1 CrewAI计划增强向量DB集成，说明方向正确

### 5.5 Mem0 Graph Memory (2026最新)

```
传统向量记忆:
  用户 → "喜欢咖啡" (孤立fact)

图记忆:
  用户 ──喜欢──→ 咖啡
    │              │
    └──常去──→ 星巴克(中关村店)
                   │
                   └──上次──→ 2026-03-10
```

Mem0 Graph Memory特点:
- 向量搜索 + Neo4j图搜索并行执行(ThreadPoolExecutor)
- 无延迟惩罚
- 支持 Neo4j / Memgraph / Neptune / Kuzu 作为图后端
- 整体性能提升约2%（准确率），但关系推理能力显著增强

**对我们项目的影响:**
- 短期不建议引入(增加Neo4j依赖)
- 长期可考虑用于Agent间关系建模和项目知识图谱

---

## 6. 推荐方案

### 6.1 技术选型组合

```
┌─────────────────────────────────────────────────┐
│              推荐技术栈                            │
├──────────────┬──────────────────────────────────┤
│ 向量存储      │ pgvector (PostgreSQL扩展)         │
│ 全文搜索      │ tsvector + pg_textsearch (BM25)   │
│ Embedding    │ BGE-M3 (本地) / OpenAI (降级)      │
│ 记忆管理      │ 自建 + Mem0可选集成               │
│ ORM          │ SQLAlchemy 2.0 async              │
│ 迁移         │ Alembic                           │
│ 缓存         │ Redis (已有) + 内存Hot层           │
└──────────────┴──────────────────────────────────┘
```

**核心原则: 不引入新的基础设施依赖，最大化复用现有PostgreSQL**

### 6.2 数据模型设计

#### 升级后的Memory表

```sql
-- Alembic迁移: 升级memories表

-- 1. 启用pgvector扩展
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. 添加新列
ALTER TABLE memories ADD COLUMN embedding vector(1024);      -- BGE-M3 1024维
ALTER TABLE memories ADD COLUMN importance FLOAT DEFAULT 0.5; -- 重要性评分 0-1
ALTER TABLE memories ADD COLUMN access_count INTEGER DEFAULT 0; -- 访问计数
ALTER TABLE memories ADD COLUMN memory_type VARCHAR(20) DEFAULT 'episodic';
    -- episodic(情景) / semantic(语义) / procedural(程序性)
ALTER TABLE memories ADD COLUMN search_vector tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED;
    -- 'simple'配置支持中文字符级分词
ALTER TABLE memories ADD COLUMN expires_at TIMESTAMP;         -- 过期时间(可选)
ALTER TABLE memories ADD COLUMN parent_id VARCHAR(36);        -- 合并来源追踪

-- 3. 创建索引
CREATE INDEX idx_memories_embedding ON memories
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX idx_memories_search_vector ON memories
    USING gin (search_vector);

CREATE INDEX idx_memories_scope_type ON memories (scope, scope_id, memory_type);
CREATE INDEX idx_memories_importance ON memories (importance DESC);
CREATE INDEX idx_memories_expires ON memories (expires_at) WHERE expires_at IS NOT NULL;
```

#### 升级后的SQLAlchemy模型

```python
from pgvector.sqlalchemy import Vector

class MemoryModel(Base):
    __tablename__ = "memories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    scope_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)

    # --- 新增字段 ---
    embedding = mapped_column(Vector(1024), nullable=True)      # 向量
    importance: Mapped[float] = mapped_column(Float, default=0.5)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    memory_type: Mapped[str] = mapped_column(String(20), default="episodic")
    search_vector = mapped_column(TSVector)                      # 全文搜索向量
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    accessed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
```

### 6.3 搜索流程 (Hybrid Search)

```python
async def hybrid_search(
    session: AsyncSession,
    query: str,
    query_embedding: list[float],
    scope: str,
    scope_id: str,
    limit: int = 10,
    k: int = 60,  # RRF参数
) -> list[Memory]:
    """
    Hybrid Search: 向量语义搜索 + BM25全文搜索 + RRF融合
    """
    # 拉取2倍候选，RRF融合后取top-N
    candidate_limit = limit * 2

    # 1. 向量语义搜索
    vector_results = await session.execute(
        select(MemoryModel)
        .where(MemoryModel.scope == scope)
        .where(MemoryModel.scope_id == scope_id)
        .where(MemoryModel.embedding.isnot(None))
        .order_by(MemoryModel.embedding.cosine_distance(query_embedding))
        .limit(candidate_limit)
    )
    vector_ranked = vector_results.scalars().all()

    # 2. BM25全文搜索
    ts_query = func.plainto_tsquery('simple', query)
    fts_results = await session.execute(
        select(MemoryModel)
        .where(MemoryModel.scope == scope)
        .where(MemoryModel.scope_id == scope_id)
        .where(MemoryModel.search_vector.op('@@')(ts_query))
        .order_by(func.ts_rank_cd(MemoryModel.search_vector, ts_query).desc())
        .limit(candidate_limit)
    )
    fts_ranked = fts_results.scalars().all()

    # 3. Reciprocal Rank Fusion
    rrf_scores: dict[str, float] = {}
    memory_map: dict[str, MemoryModel] = {}

    for rank, mem in enumerate(vector_ranked):
        rrf_scores[mem.id] = rrf_scores.get(mem.id, 0) + 1.0 / (k + rank + 1)
        memory_map[mem.id] = mem

    for rank, mem in enumerate(fts_ranked):
        rrf_scores[mem.id] = rrf_scores.get(mem.id, 0) + 1.0 / (k + rank + 1)
        memory_map[mem.id] = mem

    # 4. 加入重要性和时效性加权
    final_scores: list[tuple[float, MemoryModel]] = []
    now = datetime.now()
    for mem_id, rrf_score in rrf_scores.items():
        mem = memory_map[mem_id]
        age_hours = (now - mem.created_at).total_seconds() / 3600
        recency = math.exp(-0.005 * age_hours)
        final_score = (
            rrf_score * 0.4            # 搜索相关性
            + mem.importance * 0.3      # 重要性
            + recency * 0.2            # 时效性
            + math.log1p(mem.access_count) / 10 * 0.1  # 访问频率
        )
        final_scores.append((final_score, mem))

    # 5. 排序返回top-N
    final_scores.sort(key=lambda x: x[0], reverse=True)
    return [mem.to_pydantic() for _, mem in final_scores[:limit]]
```

### 6.4 与现有Mem0/SQLite双后端的集成策略

#### 分阶段迁移计划

```
Phase 1 (立即): pgvector后端
├─ 新建 PgvectorMemoryBackend 实现 MemoryBackend Protocol
├─ 添加 embedding 字段到 MemoryModel
├─ 实现 Hybrid Search (向量 + FTS)
├─ 通过 ResilientMemoryBackend 做降级: pgvector → SQLite
└─ 现有代码零修改 (Protocol兼容)

Phase 2 (后续): 记忆衰减和自动维护
├─ 添加 importance / access_count / memory_type 字段
├─ 实现每日维护任务 (衰减评分、归档、合并)
├─ 记忆类型分层: episodic → semantic 自动提炼
└─ 配合Redis做Hot层TTL缓存

Phase 3 (远期/可选): Mem0深度集成
├─ Mem0配置pgvector后端，共用同一PG实例
├─ 利用Mem0的自动记忆提取能力
├─ 评估Graph Memory (Neo4j) 是否有必要
└─ 保持ResilientMemoryBackend降级能力
```

#### 新增后端类

```python
# src/aiteam/memory/backends/pgvector_backend.py

class PgvectorMemoryBackend:
    """PostgreSQL + pgvector 记忆后端.

    支持:
    - 向量语义搜索 (cosine similarity via HNSW)
    - BM25全文搜索 (tsvector + ts_rank_cd)
    - Hybrid Search (RRF融合)
    - 记忆衰减评分
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        embedder: EmbeddingProvider,  # BGE-M3 或 OpenAI
    ):
        self._session_factory = session_factory
        self._embedder = embedder

    async def create(self, scope, scope_id, content, metadata=None) -> Memory:
        # 1. 生成embedding
        # 2. LLM/规则评估importance
        # 3. 存储到PostgreSQL(含embedding和importance)
        ...

    async def search(self, scope, scope_id, query, limit=5) -> list[Memory]:
        # Hybrid Search实现
        ...
```

#### 架构层次关系

```
MemoryStore (三温度管理，不变)
    │
    ├─ Hot层: 内存缓存 dict (不变)
    │
    ├─ Warm层: MemoryBackend (Protocol，不变)
    │   │
    │   ├─ PgvectorMemoryBackend (新增，推荐)
    │   │   ├─ 向量搜索: pgvector HNSW
    │   │   ├─ 全文搜索: tsvector + BM25
    │   │   └─ Hybrid: RRF融合
    │   │
    │   ├─ SqliteMemoryBackend (保留，降级用)
    │   │
    │   ├─ Mem0MemoryBackend (保留，可选高级功能)
    │   │
    │   └─ ResilientMemoryBackend (不变，降级保障)
    │       primary: PgvectorMemoryBackend
    │       fallback: SqliteMemoryBackend
    │
    └─ Cold层: JSON归档 → 未来可升级为PG归档表
```

### 6.5 Embedding Provider抽象

```python
# src/aiteam/memory/embedding.py

from typing import Protocol

class EmbeddingProvider(Protocol):
    """Embedding提供者抽象接口."""

    async def embed(self, text: str) -> list[float]:
        """将文本转换为向量."""
        ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量转换."""
        ...

    @property
    def dimensions(self) -> int:
        """向量维度."""
        ...


class BGEm3Embedder:
    """BGE-M3本地Embedding (通过sentence-transformers)."""
    dimensions = 1024

    def __init__(self):
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer('BAAI/bge-m3')

    async def embed(self, text: str) -> list[float]:
        return self._model.encode(text, normalize_embeddings=True).tolist()


class OpenAIEmbedder:
    """OpenAI API Embedding (降级方案)."""
    dimensions = 1536

    async def embed(self, text: str) -> list[float]:
        from openai import AsyncOpenAI
        client = AsyncOpenAI()
        resp = await client.embeddings.create(
            model="text-embedding-3-small", input=text
        )
        return resp.data[0].embedding
```

### 6.6 依赖变更

```toml
# pyproject.toml 新增依赖
[project.optional-dependencies]
full = [
    # ... 现有依赖 ...
    "pgvector>=0.3.0",          # SQLAlchemy pgvector支持
    "sentence-transformers>=3.0", # BGE-M3本地embedding (可选)
]
```

### 6.7 配置管理

```yaml
# config/memory.yaml
memory:
  # Embedding配置
  embedding:
    provider: "bge-m3"          # bge-m3 | openai | voyage
    dimensions: 1024
    # OpenAI降级配置
    openai_model: "text-embedding-3-small"
    openai_dimensions: 1536

  # 搜索配置
  search:
    mode: "hybrid"              # hybrid | vector | keyword
    rrf_k: 60                   # RRF融合参数
    candidate_multiplier: 2     # 候选数倍数
    default_limit: 10

  # 衰减配置
  decay:
    enabled: true
    lambda: 0.005               # 衰减率 (半衰期约6天)
    importance_threshold: 0.3   # 低于此分归档
    archive_age_days: 90        # 超龄自动归档
    merge_similarity: 0.95      # 合并相似度阈值

  # 维护计划
  maintenance:
    enabled: true
    schedule: "0 3 * * *"       # 每天凌晨3点
    max_archive_per_run: 100
    max_merge_per_run: 50
```

---

## 附录: 方案总结

### 决策矩阵

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 向量存储 | pgvector | 零新增基础设施，2026性能已达生产级 |
| 全文搜索 | tsvector + BM25 | PostgreSQL原生，与pgvector同库 |
| Embedding模型 | BGE-M3 (本地首选) | 中文SOTA，免费，1024维 |
| Embedding降级 | OpenAI text-embedding-3-small | API调用简单，无GPU时使用 |
| 搜索策略 | Hybrid (RRF融合) | 62% → 84% 精度提升已验证 |
| 记忆管理 | 自建 + Mem0可选 | 保持架构简洁，Mem0做高级补充 |
| 衰减策略 | 指数衰减 + 重要性 + 频率 | 学术验证的多维评分 |
| 迁移策略 | 分3阶段渐进 | 每阶段独立交付，风险可控 |

### 工作量估算

| 阶段 | 工作量 | 交付物 |
|------|--------|--------|
| Phase 1 | 3-5天 | PgvectorMemoryBackend + Hybrid Search + 迁移脚本 |
| Phase 2 | 3-4天 | 衰减系统 + 维护任务 + 记忆类型分层 |
| Phase 3 | 2-3天 | Mem0 pgvector集成 + Graph Memory评估 |

---

## Sources

- [Best Vector Databases in 2026 (Firecrawl)](https://www.firecrawl.dev/blog/best-vector-databases)
- [The 7 Best Vector Databases in 2026 (DataCamp)](https://www.datacamp.com/blog/the-top-5-vector-databases)
- [pgvector 0.8.0 on Aurora PostgreSQL (AWS)](https://aws.amazon.com/blogs/database/supercharging-vector-search-performance-and-relevance-with-pgvector-0-8-0-on-amazon-aurora-postgresql/)
- [pgvector Key Features 2026 Guide (Instaclustr)](https://www.instaclustr.com/education/vector-database/pgvector-key-features-tutorial-and-pros-and-cons-2026-guide/)
- [Mem0 Research: 26% Accuracy Boost](https://mem0.ai/research)
- [Mem0 Graph Memory for AI Agents (2026)](https://mem0.ai/blog/graph-memory-solutions-ai-agents)
- [Mem0 Series A ($24M)](https://techcrunch.com/2025/10/28/mem0-raises-24m-from-yc-peak-xv-and-basis-set-to-build-the-memory-layer-for-ai-apps/)
- [Mem0 pgvector Configuration](https://docs.mem0.ai/components/vectordbs/dbs/pgvector)
- [Self-Host Mem0 on Docker](https://mem0.ai/blog/self-host-mem0-docker)
- [BGE-M3 Model (Hugging Face)](https://huggingface.co/BAAI/bge-m3)
- [Best Open-Source Embedding Models 2026 (BentoML)](https://www.bentoml.com/blog/a-guide-to-open-source-embedding-models)
- [13 Best Embedding Models 2026](https://elephas.app/blog/best-embedding-models)
- [Anthropic Embeddings Documentation](https://platform.claude.com/docs/en/build-with-claude/embeddings)
- [Letta (MemGPT) Documentation](https://docs.letta.com/concepts/memgpt/)
- [Letta V1 Agent Architecture](https://www.letta.com/blog/letta-v1-agent)
- [Stateful AI Agents: Letta Memory Models](https://medium.com/@piyush.jhamb4u/stateful-ai-agents-a-deep-dive-into-letta-memgpt-memory-models-a2ffc01a7ea1)
- [LangChain Memory Overview](https://docs.langchain.com/oss/python/concepts/memory)
- [CrewAI Memory System](https://docs.crewai.com/en/concepts/memory)
- [CrewAI Cognitive Memory Architecture](https://blog.crewai.com/how-we-built-cognitive-memory-for-agentic-systems/)
- [Memory Decay and Importance Scoring](https://www.marktechpost.com/2025/11/02/how-to-design-a-persistent-memory-and-personalized-agentic-ai-system-with-decay-and-self-evaluation/)
- [Memory in the Age of AI Agents Survey](https://arxiv.org/abs/2512.13564)
- [Hybrid Search in PostgreSQL (ParadeDB)](https://www.paradedb.com/blog/hybrid-search-in-postgresql-the-missing-manual)
- [Building Hybrid Search for RAG with RRF](https://dev.to/lpossamai/building-hybrid-search-for-rag-combining-pgvector-and-full-text-search-with-reciprocal-rank-fusion-6nk)
- [pg_textsearch BM25 for PostgreSQL](https://www.tigerdata.com/blog/introducing-pg_textsearch-true-bm25-ranking-hybrid-retrieval-postgres)
- [Voyage AI Pricing](https://docs.voyageai.com/docs/pricing)
- [Vector Database Comparison 2025 (LiquidMetal AI)](https://liquidmetal.ai/casesAndBlogs/vector-comparison/)
- [ChromaDB vs pgvector Benchmark](https://github.com/Devparihar5/chromdb-vs-pgvector-benchmark)
- [What's Changing in Vector Databases in 2026](https://dev.to/actiandev/whats-changing-in-vector-databases-in-2026-3pbo)
