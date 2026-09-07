# Codex 证据夹具

本仓**第一份磁盘夹具**。此前所有测试数据都在代码里内联构造；Codex 兼容线需要对着真实的
rollout / hook / state 载荷做解析，内联构造既写不出也不可信，所以在这里开一份磁盘夹具。
除本目录外不要再新增磁盘夹具目录，扩充一律并入这里。

全部内容由 `scripts/redact_codex_fixture.py` 从本机真实 Codex 环境确定性生成，**禁止手工编辑**。

## 采集与来源

采集日期：**2026-09-02**（探针复测同日；用户环境 rollout 覆盖 2026-07-14 ~ 2026-07-24）。

| 来源类别 | cli_version | 份数 | 说明 |
| --- | --- | --- | --- |
| `native` | 0.145.0-alpha.30 | 25 | 用户 CODEX_HOME 原生会话（含 archived_sessions 内 5 份） |
| `native` | 0.146.0-alpha.3.1 | 2 | 用户 CODEX_HOME 原生会话 |
| `native` | 0.142.0 | 1 | 唯一一份 `originator=codex_exec` / `source=exec` 的原生会话 |
| `import-trimmed` | 0.145.0-alpha.30 | 2 | 外部导入会话的裁剪版（见下） |
| `probe-rollout-probe-0142` | 0.142.0 | 10 | 探针 CODEX_HOME 的 rollout |
| `probe-rollout-probe-0152` | 0.152.1 | 10 | 探针 CODEX_HOME 的 rollout（复测） |
| `hook-capture-probe-0142` | 0.142.0 | 9 | hook / notify / exec --json 载荷抓取 |
| `hook-capture-probe-0152` | 0.152.1 | 9 | 同上 |
| `hooks-config` / `hooks-trust-state` | — | 2 | 本机 `hooks.json`（仅 OS 自有条目）与 `config.toml [hooks.state]` |
| `state-db-user` / `state-db-probe-0152` | — | 2 | `state_5.sqlite` 抽样 |
| `os-event-log` | — | 2 | AI Team OS 事件库导出 |

**与任务书计数的两处偏差（刻意收录，不是漏筛）**：

1. 任务书写 27 份原生（25×0.145 + 2×0.146）。磁盘上另有 1 份 `0.142.0` / `codex_exec` 原生会话
   （8 行、44 KB），是全库唯一的 exec 入口原生 rollout，丢掉就再也采不回来，故一并收录，
   `native` 因此是 **28** 份。
2. 任务书只在 `probe-0152` 下列了 `exec-json-stream.jsonl`；`probe-0142` 下也存在同名文件
   （1.1 KB，0.142 的 exec 流形状与 0.152 不同），一并收录。

`hook-capture.jsonl` 与 `capture-run8-compact.jsonl` 内容 sha256 完全相同，**不重复收录**；
被跳过的事实与源文件 sha256 记在 `MANIFEST.json.duplicate_skipped`。

导入自检：全库 rollout 中「任一行 `payload.turn_id` 以 `external-import-turn-` 开头」命中 **32** 份，
等于 `external_agent_session_imports.json` 的 `records` 数 32，
记在 `MANIFEST.json` 的 `import_detect_hits` / `external_import_records` / `import_selfcheck_ok`。
32 份里只留 2 份裁剪版入库，其余 30 份不入库。

## 目录布局与 `/codexhome` 替换规则

夹具里所有绝对路径都已归一。**每个样本各有一个 `/codexhome` 替换根**，替换后即可在夹具内定位到真实文件：

| 替换根（`MANIFEST.files[].codexhome_root`） | 适用样本 |
| --- | --- |
| `rollouts/native` | `rollouts/native/**`、`state/threads.sample.json` |
| `rollouts/probe-0142` | `rollouts/probe-0142/**`、`hooks/probe-0142/**` |
| `rollouts/probe-0152` | `rollouts/probe-0152/**`、`hooks/probe-0152/**`、`state/threads.sample-0152.json` |

用法：把样本里的 `transcript_path` / `agent_transcript_path` / `rollout_path` 的 `/codexhome`
前缀换成上表的替换根，就得到夹具内的真实相对路径（basename 保持原样）。
现有 124 处路径（`transcript_path`/`agent_transcript_path`/`rollout_path` 三键、`/codexhome` 前缀）全部可解析。注意 `state/` 两份抽样的替换根只写在上表里、**没有**落进 `MANIFEST.files[].codexhome_root`，夹具测试对这类文件放宽为「在任一替换根下能解析到」。

`/codexhome` **内部**同样只有结构目录保留尾部：`sessions/`、`archived_sessions/`（H5 要求可解析）
与 `hooks/ai-team-os/`（本仓自带的 hook 脚本）保留完整尾部；其余子树只保留顶层目录名并把尾部折成
`<path:K>`（如 `/codexhome/visualizations/<path:4>`），因为 CODEX_HOME 下的产物文件名会带出
文档标题与私有脚本名——与工作区内部文件名同一条理由。顶层目录名本身取 Codex 产品自有名的白名单，
不在白名单内的整条折成 `/codexhome/<path:K>`。

其余路径占位：

- `/workspace/probe` = 探针工作区；`/workspace/proj-NN` = 真实项目工作区（同一原路径同一序号，按原路径字典序编号）。
  工作区**内部**的文件名一律折成 `/workspace/proj-NN/<path:K>`（K = 剩余路径段数），因为项目内文件名会带出工具名与文档名。
- `/root`、`/root/agent-NN` = agent 身份路径（保留 `/root` 树形与深度，名字换掉）。
- 其他路径 → `<path:K>`。

## 脱敏规则

规则源码唯一落点是 `scripts/redact_codex_fixture.py`，本节只是摘要（H 编号对齐任务书）。

- 基线：非结构字符串一律 `<str:N>`（N = 原字符数）；`KEEP_KEYS` 白名单内的枚举/ID/时间戳原样保留。
- **H1** `session_meta.payload.base_instructions`、`session_meta.payload.git` 整键删除。
- **H2** 会话正文**整行删除**：`response_item` 的 `message` / `reasoning`；`event_msg` 的
  `agent_message` / `user_message` / `agent_reasoning` 及其 `*_delta`、`raw_content` 变体。
- **H3** 模型 slug 全量映射为 `codex-model-a` / `-b` / …（按首次出现顺序稳定编号，映射表只在运行期存在）。
- **H4** `timezone` 一律改 `UTC`。
- **H5** 路径归一，规则见上一节。
- **H6** `tool_response` 中 ≥ 16384 字符的大字符串：**保留精确字节长度**，内容换 `<filler>` 重复串（可压缩、不截断）。
  filler 是纯 ASCII，故夹具里的字符数就是原串字节数；原串含多字节字符时会与 `<str:N>` 的**字符数**口径相差几个数——
  当前命中 3 处，原串各 40106 字符 / 40110 字节，夹具里是 40110 个字符。其余内容键走 `<str:N>`。
- **H7** Fernet 形制密文（`gAAAAA…`）原样保留，共 40 处（派工正文 `message` 12 处 + agent 间通讯 `encrypted_content` 28 处）：
  密文已加密且密钥不在本仓，保留它们才能写出「派工正文在 Codex 侧不可读」这条断言。其余：UUIDv7 / `call_*` / `turn_id` / 时间戳 / 数值 / 布尔原样保留。
- **H8** `arguments` / `tool_input` / `tool_response` 等 JSON 串：解析后保留键骨架、值按 `<str:N>`；解析失败整串 `<str:N>`。
- **H9** `compacted.payload.message` → 等长 filler（按字节，与 H6 同口径；filler 在脱敏遍历**之后**覆盖，
  否则会被 `message` 的内容键规则改成 `<str:N>`），保留 `window_number` / `first_window_id` /
  `previous_window_id` / `window_id`；`world_state` 内 `agents_md` 等内容键 → `<str:N>`；
  `task_complete.last_agent_message` → `<str:N>`。
- **H10** 导入裁剪版：全部内容字符串 → `<str:N>`，只白名单保留精确字面量 `<EXTERNAL SESSION IMPORTED>`
  （导入段边界标记，因此承载它的那一行 `event_msg/agent_message` 破例不删）；
  `external-import-turn-*` 前缀的 `turn_id` 天然保留。
- **H11** 密钥零收录：`auth.json`、`OPENAI_API_KEY`、`experimental_bearer_token`、`sk-` 串一律不进入；
  `config.toml` 只读 `[hooks.state]` 段；`hooks.json` 只保留 OS 自有条目（`hooks/ai-team-os/` 下脚本），第三方条目整条删除。
- **H12** 确定性：不写生成时间戳，文件遍历排序固定，JSON 键序随源、`ensure_ascii=False`、紧凑分隔符。
  同一输入两次运行产物逐字节相同（`diff -r` 验证）。

另有两条实现层收紧，同样是脱敏的一部分：

- 身份键（`agent_path` / `author` / `recipient` / `task_name` / `agent_name` / `agent_nickname` / `sender` / `owner` …）
  只保留 `/root` 树形，其余一律 `<str:N>`——这些位置曾带出第三方工具名。
  `agent_nickname` 目前装的是 Codex 自动分配的昵称、不含用户身份，仍按身份键处理：一旦允许用户自定义，
  同一个字段就变成身份泄漏通道，不留这个口子。
- `KEEP_KEYS` 内的值若内嵌绝对路径（`sandbox_policy` 这类策略串可能带自定义可写根），一律降级为 `<str:N>`，
  不因为它在白名单里就整串放行。
- 会重复整段历史的列表（`replacement_history` / `results` / `entries` / `content` /
  `dynamic_tools` / `tools` / `oneOf` / `required` …）截断为前 3 个元素 + `<+N>` 计数标记。

生成器内置**泄漏守卫**：写盘前逐文件检查两类东西，命中即抛错中止（报错不回显命中内容），绝不静默写出。

- 字面量：家目录、`/Users/`、`/Volumes/`、真实模型 slug、`base_instructions`、`Asia/` 等；
  站点专有字面量（雇主名、内部工具名）由环境变量 `CODEX_FIXTURE_FORBIDDEN`（逗号分隔）在运行期注入，
  **不写进脚本**，注入值也不会出现在报错信息里。
- 形制：**非 ASCII 字符**、邮箱、`http(s)://`、`sk-` 形制密钥、`bearer` 值。
  非 ASCII 这条是关键——本批唯一一次真实内容泄漏（CODEX_HOME 下的中文文档名）就是从「守卫只认字面量」这条缝出去的，
  夹具测试里另有一条同名断言做第二道闸（`README.md` / `golden.json` 是中文说明文件，不在此列）。

## 再生成

```bash
python3 scripts/redact_codex_fixture.py \
  --evidence <codex-research-20260902 目录> \
  [--codex-home <CODEX_HOME，默认 ~/.codex>] \
  [--os-db <aiteam.db，默认 ~/.claude/data/ai-team-os/aiteam.db>] \
  [--out tests/fixtures/codex]
```

脚本只读原件（sqlite 一律先复制到临时目录再以 `mode=ro` 打开），输出每个文件的 sha256。
验证确定性：换一个 `--out` 再跑一次，`diff -r` 两份产物应无差异。

## MANIFEST.json

`files[]` 每项：`path` / `sha256` / `bytes` / `lines` / `source_class` / `source_ref`（仅原文件 basename）/
`codexhome_root` / `kept_row_types`（计数）/ `dropped_row_types`（计数）/ `trim_rule`。
**`kept_row_types` / `dropped_row_types` 是脱敏阶段口径（裁剪之前）**，不是磁盘上这份文件的行清单：
被 `trim_rule` 裁过的文件（两份导入样本 + 1 份 0.146）磁盘行数会远小于 `kept_row_types` 合计。
顶层 `row_type_counts_scope` 也写着这一点。
顶层还有 `import_samples`、`import_detect_hits`、`external_import_records`、`import_selfcheck_ok`、
`unknown_row_types`（未知 `type`/`payload.type` 计数，当前为空）、
`unknown_payload_keys`（未在白/黑名单内的 payload 键计数，仅用于观测，值已按未知键规则脱敏）、
`duplicate_skipped`、`model_placeholders`、`generator`（脚本路径 + sha256）。

## golden 索引表

`golden.json` 由 `scripts/compute_codex_golden.py` 从**原件**重算生成（不读夹具作为真源），
`tests/unit/test_codex_fixtures.py` 再用一份独立最小实现在夹具切片上复算一遍。

每项结构：`name` / `sources`（原件 basename 或 `os-db` / `state_5` / `imports.json`）/
`fixture_files` / `method`（可复现方法与判据）/ `values`（**生产口径**，全量原件）/
`fixture_verifiable`，可复算项另有 `fixture_values`（**夹具口径**：夹具只收了 60 份原生里的 28 份
且两份导入样本被裁过行，所以行号与合计值与生产口径必然不同，同名键各按各的口径读）。

| golden | 依赖夹具文件 | 断言 |
| --- | --- | --- |
| `G1_import_then_resume` | `rollouts/native/…019f8d58-610c….jsonl` | 导入段（`turn_id` 前缀 `external-import-turn-`）末条幻影 `token_count` 是导入基线；此后每条真值满足 `total-(input+output)==基线`，违例 0 |
| `G2_import_never_resumed` | `rollouts/native/…019f8d58-606e….jsonl` | 导入段延伸到文件末尾、其后无行、全份无真值 `token_count`——导入后从未被续用 |
| `G3_fork_replay` | `…019f8d92-93fd….jsonl` + `…019f8b2f-1617….jsonl` | fork 份末条真值 total 与 `forked_from_id` 指向的父份相等 ⇒ 是重放副本，不得二次入账 |
| `G4_self_fork` | `…019f8b21-a6ae….jsonl` | 子线程继承父 `session_id`，`forked_from_id==session_id` 而 `!=id` ⇒ 按 session 归档时的假自指，不判重放 |
| `G5_ctx_sentinel` | `…019f8d1a-c148….jsonl` | 五层全零且 `total==info.model_context_window` 的是窗口哨兵行不是用量；真值取最后一条非哨兵 |
| `G6_subagent_id_trap` | `…019f8a63-ef67….jsonl` | `payload.id != payload.session_id`，`parent_thread_id` 指父，`source={"subagent":{"other":"guardian"}}` |
| `G7_mcp_call_shape` | `…019f8d95-d818….jsonl` | `response_item` 的 `custom_tool_call`/`function_call` 与 `event_msg/mcp_tool_call_end` 是两套独立计数 |
| `G8_system_session` | `os-events/019f8d5f.jsonl` | 只有一条 `cc.session_start`、零工具事件、cwd 在 `/codexhome/memories` ⇒ Codex 内务会话 |
| `G9_unpersisted_session` | `os-events/019f99a6.jsonl` | 有成串 `cc.tool_use`，但 rollout 与线程表里都没有这个 id ⇒ 以 rollout 为唯一真源必漏 |
| `G10_hook_payload_shape` | `hooks/probe-0142/capture-run4-subagent.jsonl`、`hooks/probe-0152/…` | `SubagentStart.transcript_path` 指子、`SubagentStop.transcript_path` 指父而 `agent_transcript_path` 指子；派工工具名 0.142 无命名空间前缀、0.152.1 有 |
| `G11_subagent_reach` | `state/threads.sample.json` | `source` 含 `subagent.thread_spawn` 的线程 = `thread_spawn_edges` 的子线程；`subagent.other` 为免检桶 |
| `G12_baseline_0142` | `rollouts/probe-0142/**`（10 份） | 各份末条 `total_token_usage` 三元组；run8 压缩用例的累计值不因 compact 回落 |
| `G13_phantom_partition` | `rollouts/native/**`（30 份） | 幻影行被「哨兵」与「导入基线重发」两条判据穷尽，残余必须为 0 |
| `G14_fork_lineage_dedup` | `rollouts/native/**`（30 份） | 真 fork（排除假自指）按「首条真值 total 相同即同谱系」去重后与朴素求和的虚增比例 |
| `G15_import_predicate_count` | `MANIFEST.json` | 夹具内命中导入判据的 rollout 数 == `MANIFEST.import_samples`（生产口径另与 `imports.json` 的 records 数互证） |

`golden.json` 与 `README.md` 一样**不登记进 `MANIFEST.files`**（`MANIFEST` 只记生成器产出的样本文件），
生成器会跳过这两个文件，重跑不覆盖；夹具完整性测试把它们列在允许的未登记文件里。

重算：

```bash
python3 scripts/compute_codex_golden.py --evidence <codex-research-20260902 目录>
```

## 体积纪律

**新增夹具须先合并再入库；单文件超过 100 KB 须在本 README 列出并说明理由。**

当前全套 77 个文件、约 **3.28 MB**（3,437,183 字节），超过 600 KB 的目标。
根因：golden 断言依赖的 10 份原生 rollout **不得裁行**，而它们剩下的行几乎全是结构行
（`event_msg/token_count` 约占 39%，`turn_context`、`custom_tool_call(_output)` 又占 35%），
这些行的体积就是证据本身，只能靠删行缩小。是否放宽「golden 不裁行」需缔造者裁定。

非 golden 的原生份若骨架化后仍 > 100 KB，按 `MANIFEST.trim_rule` 登记的规则裁剪：
`head40 + tail40 + 该文件内出现次数 ≤ 40 的所有行类型的全部行`——
高频记账行按首尾取样，稀有结构行一行不丢。当前只有 1 份命中（`019f93ea-9368`，0.146）。
两份导入裁剪版的规则分别是 `head60+tail6(back-to-last-token_count)`（实际 66 行）与 `head40`（实际 40 行）。

### 超过 100 KB 的文件（全部是 golden 依赖的原生份，禁止裁行）

| 文件 | 字节 |
| --- | --- |
| `rollouts/native/sessions/2026/07/23/rollout-…-019f8d95-d818-….jsonl` | 475,679 |
| `rollouts/native/sessions/2026/07/23/rollout-…-019f8d92-93fd-….jsonl` | 383,649 |
| `rollouts/native/sessions/2026/07/23/rollout-…-019f8b2f-1617-….jsonl` | 374,175 |
| `rollouts/native/sessions/2026/07/22/rollout-…-019f8a5c-8a8b-….jsonl` | 357,071 |
| `rollouts/native/archived_sessions/rollout-…-019f8d7f-9314-….jsonl` | 239,715 |
| `rollouts/native/sessions/2026/07/23/rollout-…-019f8d1a-c148-….jsonl` | 237,803 |
| `rollouts/native/sessions/2026/07/23/rollout-…-019f8b21-a6ae-….jsonl` | 231,985 |
| `rollouts/native/sessions/2026/07/23/rollout-…-019f8b4d-1b95-….jsonl` | 217,625 |

理由一致：这 8 份是 golden 断言的取值来源（含两组 `thread_spawn` 父子链、compaction、
guardian 子会话、archived 会话），裁行会让断言失去依据。

## 使用约定

- 入口常量在 `tests/testlib.py`：`CODEX_FIXTURES`。
- **禁止**在 `tests/conftest.py` 里加 autouse 夹具去预加载本目录：3 MB 的 JSONL 会被每个
  测试进程无条件读一遍，拖慢全量跑且与本目录无关的用例也会被它的失败拖垮。
  需要时在用例内显式读取 `CODEX_FIXTURES / "<相对路径>"`。
