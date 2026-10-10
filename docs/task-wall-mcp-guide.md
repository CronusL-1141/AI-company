# 任务墙 MCP 日常管理入参说明

本页描述src/aiteam/mcp/tools/task.py与HTTP schema的日常调用契约。实际部署版本和客户端缓存可能不同，调用前先发现当前工具参数；升级后刷新MCP会话再使用新增参数。

## 完整日常参数

表内默认值可省略。ID使用真实完整OS UUID。标题前缀、parent:与blocked-by:标签不产生结构关系，本批不自动迁移旧标签。

| 工具 | 必填 | 可选参数与默认值 |
| --- | --- | --- |
| context_resolve | 无 | 无 |
| task_create | title | project_id=""、description=""、priority="medium"、horizon="mid"、tags=null、auto_start=false、task_type=""、parent_id=""、depends_on=null、assigned_to=""、waiting_on=null、status_note=""、fields="all" |
| task_run | team_id、description | title=""、model=null、depends_on=null、priority=""、horizon=""、tags=null、assigned_to=""、parent_id=""、auto_start=false、waiting_on=null、status_note=""、fields="all" |
| task_create_batch | tasks、idempotency_key | project_id=""、fields="all" |
| task_update | task_id | status=null、assigned_to=null、result=null、priority=null、tags=null、title=null、description=null、horizon=null、parent_id=null、depends_on=null、waiting_on=null、status_note=null、expected_revision=null、fields="all" |
| task_list_project | 无 | project_id=""、team_id=""、horizon=""、priority=""、limit=50、offset=0、include_completed=false、status=""、fields="compact" |
| task_status | task_id | 无 |
| task_memo_add | task_id、content | memo_type="progress"、author="leader"、supersedes=null |
| task_memo_read | task_id | 无 |
| task_execution_trace | task_id | include_stats=false |
| verify_completion | task_id | 无 |
| unified_search | query（1–200字符） | limit=10（1–50）、project_id="" |
| usage_reconciliation | 无 | project_id=""、source_namespace=""、since=""、until=""、limit=500（1-500） |

其他查询：link_query(kind,id,direction="both",limit=50)；link_trace(kind,id,depth=2,limit=50)。引用边不是父子或依赖。

枚举：status=pending/running/blocked/completed/failed；priority=critical/high/medium/low；horizon=short/mid/long；memo_type=progress/decision/issue/summary；fields=compact/all（兼容full）。写入默认all，日常可用compact缩小回执。

## 创建、关系与等待

task_create创建项目任务，不要求团队；空project_id解析当前项目。task_run创建团队任务，HTTP缺省priority=medium、horizon=short。auto_start只请求运行状态，不派工；未完成依赖或明确等待返回真实blocked。assigned_to不会启动agent；task_type已退役，model不是派工开关。

HTTP项目创建或批量条目显式写status=running时采用严格启动语义：未完成依赖或未解除等待返回409，整批不写入；同时给auto_start=true也不改变这条约束。省略显式running、仅请求auto_start仍按实际条件推导blocked。MCP单条task_create/task_run没有status入参；批量条目可提供status。

parent_id表达范围归属，depends_on表达前置顺序。仅限同项目真实任务；父子仅一层，拒绝自己、环、跨项目、向已完成父任务新挂子任务。依赖完成只解除明确dependency阻塞，不解除人工或用户等待。

waiting_on结构：{"kind":"user或agent","target_id":"等待user时可省略；等待agent时必须是同项目真实agent ID","reason":"必填，1-2000字符"}。任务前置使用depends_on；status_note写当前状态说明。

```text
context_resolve()
task_create(title="P2 收口", project_id="<项目 UUID>",
            priority="high", horizon="short", fields="compact")
task_create(title="接口验收", project_id="<项目 UUID>", parent_id="<父任务 UUID>",
            depends_on=["<前置任务 UUID>"], auto_start=true, fields="compact")
task_update(task_id="<任务 UUID>", waiting_on={"kind":"user","reason":"确认验收范围"},
            expected_revision=1, fields="compact")
```

批量为1–100条项目任务。每条必填key（本批唯一，1–100字符）和title；可选description、priority、horizon、tags、assigned_to、status（默认pending）、parent_id、depends_on、auto_start、waiting_on、status_note，以及保留的cc_task_id/cc_blocked_by桥接元数据。普通管理不需要桥接元数据。

parent_id/depends_on接受真实UUID或本批@key。idempotency_key长1–200字符；同键同参数重放原结果，换参数返回冲突；整批原子写入。

```text
task_create_batch(project_id="<项目 UUID>", idempotency_key="p2-acceptance-v1", fields="compact",
  tasks=[{"key":"parent","title":"P2 收口"},
         {"key":"api","title":"接口验收","parent_id":"@parent"},
         {"key":"ui","title":"页面验收","parent_id":"@parent","depends_on":["@api"]}])
```

## 更新、清空与并发

只传本次实际修改的字段。MCP省略/null保持原值；description/result/status_note/assigned_to的空字符串清空对应内容，tags=[]和depends_on=[]清空数组；parent_id=""解绑、waiting_on={}解除等待。HTTP中parent_id/waiting_on对应显式null。title不能为空，priority/horizon/status不接受空字符串。

先task_status读revision，写时带expected_revision。409返回current_revision/current_task；重读核对后只重试本次修改，不把旧全字段覆盖新记录。不传revision兼容旧调用，但没有冲突保护。内部解锁和状态更新也递增版本；旧记录首次有效更新才建立版本元数据。

```text
task_status(task_id="<任务 UUID>")
task_update(task_id="<任务 UUID>", horizon="short", assigned_to="", description="",
            parent_id="", depends_on=[], waiting_on={}, expected_revision=3, fields="compact")
```

显式status=blocked表示人工阻塞。解除人工阻塞须明确改状态；清除waiting_on不忽略依赖。未开始/已阻塞任务存在等待或未完成依赖时，启动请求会被拒绝；同一请求可解除等待并请求running。运行中的既有依赖若被重开，无关编辑保留running并提示，不当成重新开始；新增冲突依赖需先明确暂停。旧blocked无原因时保持原状态并提示，不猜测历史原因。

## 看墙与收尾

task_status是全字段详情和revision，不是子任务列表。task_list_project默认compact；fields=all可读取父卡片subtasks。墙/摘要新增subtask_progress、latest_progress、waiting_user、executor_stopped及management_hints；执行者停止不自动完成任务。

项目墙limit只限制pending行，running/blocked/failed全返；stats/digest是全墙顶层口径。team_id非空则读团队墙，忽略project_id；团队stats包含子任务。团队墙用真实UUID，没有task_run的队名解析。完成筛选仍需include_completed=true；MCP尚未暴露HTTP completed_limit/offset，不能把limit当完成记录分页。

```text
task_list_project(project_id="<项目 UUID>", limit=20)
task_list_project(project_id="<项目 UUID>", fields="all")
task_memo_read(task_id="<任务 UUID>")
task_memo_add(task_id="<任务 UUID>", memo_type="progress", author="<实际 agent 名>",
              content="已完成事项、证据和剩余工作")
task_memo_add(task_id="<任务 UUID>", memo_type="summary", author="<实际 agent 名>",
              content="交付路径、实际测试结果与未部署范围")
task_update(task_id="<任务 UUID>", status="completed", result="交付摘要",
            expected_revision=4, fields="compact")
verify_completion(task_id="<任务 UUID>")
```

普通completed更新和HTTP complete共用逻辑：检查未关子、未完成依赖及明确等待，冲突列出处理对象；同事务解锁下游。failed子任务仍算未关事项，需要解决失败或明确调整范围/关系。重复完成可补偿检查，保留首次完成时间；历史完成时间未知时仍保持未知。历史已完成记录的旧矛盾不阻止普通元数据编辑，但会警告，不能新增矛盾关系。

verify_completion仅检查completed和memo/summary齐备，不能替代代码、业务测试或部署验收。memo的supersedes使用真实旧memo ID；author为实际agent。报告默认项目范围，跨项目找用户指定报告可先unified_search再report_read。

## 用量对账与生效范围

usage_reconciliation仅读已保存证据，不触发模型、认证读取或全盘扫描。since/until必须带时区；limit是每来源最多500条的扫描上限，MCP只给摘要与每桶最多3个样例，响应上限16KB。必须一起看scope/counts/sample_limit/truncated，不能当全账号覆盖率；桶计数是有限扫描中的证据，来源总数另列。桶详情使用HTTP的bucket_offset/bucket_limit（每桶最多50条）分页。journal未知归属不按当前项目强行归因。历史未留证据的ephemeral不能恢复。

```text
usage_reconciliation(project_id="<项目 UUID>",
  since="2026-07-01T00:00:00-07:00", until="2026-07-02T00:00:00-07:00", limit=500)
```

接入命令见src/aiteam/data/USAGE.external-exec.md。task_create_batch与usage_reconciliation需要对应版本服务；部署后以新会话实际schema验证生效。
