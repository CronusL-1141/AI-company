# AI Team OS

**技术栈**: Python 3.12 + FastAPI | React 19 + Vite | SQLite
**架构**: Storage → API → Dashboard（详见 docs/architecture.md）

## 核心约束
- 所有输出使用中文
- 共享类型只引用 `src/aiteam/types.py`
- 代码风格: PEP 8，类型注解，async优先

## Leader核心行为
- 专注统筹，实施工作委派团队成员
- 新需求先加入任务墙，系统级功能先写设计文档
- 完整规则通过SessionStart自动注入，也可查询 GET /api/system/rules
