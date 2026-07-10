# 生态深扫报告模板（五段式）

`ecosystem_deep_reviewer.py` 的派遣 prompt 引用本文档；`deep_review_link.py`
hook 按本模板解析 `report_save` 的报告并自动回填 deep_review 行。
**段位编号→数据库字段的映射由 hook 固定**，写报告必须遵守本顺序，
否则风险/可借鉴内容会错位入库（2026-07-10 实测踩坑：本文档曾缺失，
调研 agent 自定 3=风险/4=可借鉴，与 hook 的 3=learnings/4=risks 相反）。

## 锚行（报告正文最前两行，不加代码块包裹）

```
repo_id=<uuid>
deep_review_id=<uuid>
```

hook 靠这两行把报告绑定到 deep_review 行（`link_report`，幂等）。

## 五个二级标题（编号必须是 `## 1.` 到 `## 5.`）

| 段位 | 标题建议 | 入库字段 | 内容 |
|---|---|---|---|
| `## 1.` | 真实定位 | `summary_md` | 它是什么、解决什么问题、README 宣称 vs 实际 |
| `## 2.` | 架构解析 | `architecture_md` | 核心模块、数据流、关键设计决策（引用具体文件路径） |
| `## 3.` | 可借鉴点 | `learnings_md` | 对 AI Team OS 各子系统的具体启发 |
| `## 4.` | 风险与短板 | `risks_md` | 工程短板、维护状态、依赖负担 |
| `## 5.` | 集成建议 | `integration_md` | 必须含一行 `推荐动作: integrate|reference|learn|skip` |

`## 5.` 中的推荐动作行由 hook 正则提取到 `integration_recommendation`。

## 结尾元数据块（非数字标题，hook 不计入五段）

```
## 元数据
- demo_result: success|fail|skipped
- demo_log_excerpt: |
    <可选，缩进的日志尾部>
```

## 落库调用

```
report_save(
    author="<agent名>",
    topic="deep-review-<owner>-<repo>",
    content=<上述 markdown>,
    report_type="deep-review",
)
```

`deep_review_link.py`（PostToolUse hook，源码安装模式由 install.py 注册、
插件模式由 hooks.json 注册）会在 report_save 后自动解析五段并推进
stage_status；若 hook 未生效，可用
`POST /api/ecosystem/deep_reviews/{id}/link_report` 手动补链（幂等）。
