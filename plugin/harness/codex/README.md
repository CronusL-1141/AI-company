# Codex 适配器目录

本目录是 AI Team OS 的 **Codex harness 适配层**。它与 CC 侧的分发面并列而不相交：
一个核心（DB / API / 翻译层）+ 两个适配器，适配器之间零依赖，适配器对核心是单向依赖。

| 文件 | 是什么 | 由谁对钉 |
|---|---|---|
| `surface.py` | `CODEX_HOOK_SURFACE` 唯一真相源 + 渲染函数 + 版本常量 + 工具名三面登记表 | I15 / I-CDX-R8 |
| `hooks.json` | `render()` 的产物，**占位符形态的参照清单** | I15（与 `surface.py` 1:1） |
| `hook-trust.lock` | 注册声明的哈希（不含脚本内容），按平台分两列 | I17（由 `hooks.json` 重算比对） |
| `agents_md_header.md` | 生成 `AGENTS.md` 用的标准头 | I18 |
| `hooks/hook_core.py` | 送事件核心函数的**第三份逐字节副本** | I1（三方对钉） |
| `hooks/channel_unread_codex.py` | 按需查询本项目点名未读并注入提示，不推进已读水位 | I15 / I17 / I20 与未读测试 |

配套机检脚本住在仓库的 `scripts/` 下（`check_codex_hook_surface.py` / `check_codex_execpolicy.py` /
`check_codex_trust_lock.py` / `check_codex_isolation.py`），由 `scripts/check_invariants.sh` 编排。

## 核心工具短目录与专属角色

`session_bootstrap_codex.py` 在 SessionStart 输出名称与一句用途目录，覆盖启动、恢复和
compact 对应的原生 SessionStart；完整参数仍经宿主工具发现获取。主目录最多32条，
每次目录最多5000字符，包含统一检索；不会把任务或报告全文放进启动上下文。

`inject_subagent_context_codex.py` 在 SubagentStart 提供13个查询工具的目录，不要求额外的
task_id/project_id。显式任务绑定仍单独核验，只有核验成功才添加原派单授权下的记账提示。
子目录不列服务重启、删除或模型配置修改；**目录不是权限机制**。

目录辅助模块 `tool_catalog_codex.py` 读取 Codex home、工作目录上层及可识别角色的配置，
保守取 enabled_tools 的交集、去除 disabled_tools；禁用服务器或配置损坏时不显示目录。
它不是原生配置合并器，无法获知所有运行时覆盖，最终以当前会话实际工具目录为准。

本适配器不新增子Agent角色。权限分层应沿用现有角色；短目录不能代替实际工具过滤。
本机default角色白名单试验尚未通过：完整角色连接配置虽可加载，原生新子会话仍发现116个
OS工具，且白名单外的只读team_list可执行，因此未保留该无效连接副本。

更新已安装副本后，Hook内容在下一次对应事件生效。源码/安装检查与真实角色权限验收
必须分别报告，不能将目录不显示管理工具称为宿主已经禁止其调用。

---

## 三条纪律

### 一、本目录不被任何宿主自动加载

CC 只按约定路径寻找 `hooks/hooks.json`、`skills/`、`commands/`、`agents/` 与插件清单目录；
源码安装器的取源常量集合恒为 `{hooks, agents, skills, commands, loop.md}` 五项，**从不遍历 `plugin/harness`**。
Codex 侧同理：本目录不在它的插件约定路径上。

所以本目录里的 `hooks.json` **不会被任何一方拾取**，这是设计而不是疏漏——它是给安装器读的参照物，
不是给宿主读的注册面。I20 把这句话变成三条静态断言：本目录不出现 CC 家目录字面量、不出现
CC 全局配置文件的写入点、不引用安装器的 CC 侧符号；反向断言安装器的取源常量集合未被扩张。

**推论**：往本目录里加文件不会改变任何一方的运行时行为，但也**不会自动生效**——生效只发生在
安装器把渲染产物写到用户机器上的那一刻（P0-5）。

### 二、`hooks.json` 是占位参照，不可直投

清单里的 `{{PY}}` 与 `{{CODEX_HOOKS_DIR}}` 是占位符，不是环境变量：

- Codex 的 hook 子进程里**插件根变量与配置目录变量均未置位**（实测），命令行里写变量必然落空；
- 宿主要的是**绝对解释器路径**与**绝对脚本路径**，二者在提交时都不可知。

安装器在写盘那一刻替换这两个占位符并按平台选列（`command` / `command_windows`，后者本轮生成但未在
Windows 上验证）。**把本文件原样拷到用户配置目录 = 注册一批跑不起来的钩子**，而未授信的钩子在这个
宿主上是**静默全跳过**：不触发、不告警、连一条待授信记录都不写。所以「拷过去看看」这条路上没有任何
报错来提醒你拷错了。

I15 另断言清单里不出现任何家目录字面量：一旦有人图省事把自己机器上的绝对路径粘进来，当场红。

### 三、顺序即信任锚，只能尾部增删

宿主的授信记录以 `<清单绝对路径>:<snake_case 事件>:<group 序号>:<handler 序号>` 为键。
**序号是键的一部分**；当前宿主还核对声明内容的 `trusted_hash`（见下），因此：

- 在某个事件的中间插入一个 group，会让它之后的所有 handler 换键，可能造成批量失信；
- 删掉中间的一条同理；
- 只在**尾部**追加或从**尾部**摘除，才只影响被改的那几条。

配套两条：

1. **禁止照搬 CC 的重建式注册算法**（先删光己方条目、再按表整体追加）。那套算法在 CC 上没有代价，
   在这里等于每次安装都把全部序号重排一次。Codex 分支的安装器必须**按索引就地更新**。
2. **卸载从尾部摘**；第三方守卫条目一律不动（现场实测里有一个非本仓脚本占着首位信任槽）。

另一条实测背书：**换脚本内容不失信**。把已授信脚本的内容整体换掉而不动 `hooks.json`，宿主侧全部
handler 仍显示启用并正常触发。所以 `hook-trust.lock` 哈希的是**注册声明五元组**
（snake_case 事件、group 序号、handler 序号、渲染后 command、timeout），**绝不含脚本文件内容**——
哈希脚本内容会在每次普通代码改动上假报警，而在唯一真正让用户付出重新授信代价的改动上保持沉默。
本仓五元组锁是发布机检协议，不等同于宿主原生 `trusted_hash`，不能据此证明宿主已授信。

---

## 改动纪律：改注册面与改脚本不同批

`hook-trust.lock` 的变化标识受影响的注册声明；新增项须单独授信，旧声明不变则保留信任。
不要把新增一项误报成全体失信，也不要遗漏真正改变声明的条目。因此：

- **改 `hooks.json`（注册面）与改 hook 脚本（内容）不得进同一次发布**。
  升级序列固定两段：阶段 1 只换脚本（零失信，可放心做）；阶段 2 才改注册面。
- lock 变化的发布必须在 Release notes 里写明「本次升级需重新授信」，且授信指引**按入口分列**——
  CLI/TUI 走斜杠命令，桌面端走「设置 → 编码 → 钩子」的图形化管理面。只写前者会让桌面端用户找不到门。
- 改 `surface.py` 必须同批重新生成 `hooks.json` 与 `hook-trust.lock`（三者由 I15/I17 对钉，漏一个即红）。

**「发布前先算失信清单」不再是人肉步骤**（0916）。`hook-trust.baseline.lock` 存的是**上一次发布的
注册面**，即用户手上已经授信的那一份；`scripts/check_codex_trust_drift.py`（机检 I17b）拿它跟当前锁
对账，把每个槽位判成四类之一：

| 判定 | 含义 | 要做的事 |
| --- | --- | --- |
| 保持授信 | 槽位与命令都没变 | 无 |
| 新增待授信 | 尾部新增槽位，宿主会主动询问 | 写进 Release notes 的授信清单 |
| 已摘除 | 槽位没了，宿主自然丢弃 | 无 |
| **槽位复用** | 同一槽位换了命令 | **机检直接红**，见下 |

旧版本的现场记录曾将槽位滑位描述为「原样继承前任授信」；这不是当前宿主的通用事实。
已核对官方源码提交 `0a2eb4696c26ac33204bcd255721ab30220a4774`：宿主对标准化的事件、matcher
和单个 handler 生成 TOML fingerprint；普通用户 hook 只有保存的 `trusted_hash` 与当前指纹相等才是
`Trusted`，不相等为 `Modified`，没有记录为 `Untrusted`
（[discovery.rs](https://github.com/openai/codex/blob/0a2eb4696c26ac33204bcd255721ab30220a4774/codex-rs/hooks/src/engine/discovery.rs#L765-L810)）。

槽位复用仍会改变稳定的注册位置与授信映射，本仓 **I17b 禁止槽位复用**的工程纪律不变。
I17 核对本次清单与本次锁；I17b 额外对照上次发布基线，阻止滑位和复用。该机检不替代宿主的原生授信判断。

发布时用 `python3 scripts/check_codex_trust_drift.py --advance` 推进基线，并同批把
`released_version` 改成本次版本号。推进那一刻正是写 Release notes 授信指引的时刻——这是把两件事
绑在一起的唯一办法。

## 用户生命周期命令

使用安装好项目依赖的系统 Python，从源码 checkout 执行；`--api-url` 使用实际本机端口：

```bash
python3 scripts/codex_adapter.py install --api-url http://127.0.0.1:8000
python3 scripts/codex_adapter.py update    # 同步当前源码，沿用安装回执的模式与参数
python3 scripts/codex_adapter.py upgrade   # 干净检查、仅快进拉取、同解释器 pip、update、status
python3 scripts/codex_adapter.py update --dry-run --json # 精确文件预览，不写入
python3 scripts/codex_adapter.py status    # 检查副本、注册、MCP路径、HTTP就绪与API更新状态
python3 scripts/codex_adapter.py start     # 可选：以前台方式启动API，已运行则复用
python3 scripts/codex_adapter.py uninstall # 只预览
python3 scripts/codex_adapter.py uninstall --apply
```

`install/update` 管理所选 Codex home 内的 `config.toml` 中 OS MCP 字段、`bin/aiteam-http-headers.py`、
`hooks.json` 和 `hooks/ai-team-os-observer/`，包含 `hook_core.py` 等全部运行依赖。
目录优先级为 `--codex-home`、`CODEX_HOME`、`~/.codex`；按需运行记录默认放在该目录下的
`ai-team-os/runtime`，可用 `--runtime-dir` 指定。安装只写配置；下次 MCP 连接时 helper 才按需启动
独立 API，终端退出不停止所属 API。不安装定时器，不扫描或重启其它实例。

安装器保留已有模型/认证配置、其它 MCP、timeout、自定义 reader/project 参数与第三方 Hook 索引，
只在事件尾部追加缺失声明。配置与脚本先整体预检，写前备份，失败恢复原字节。发现用户修改过的
脚本或 helper 命令则停止覆盖。无元信息旧安装按随包发布的旧版摘要清单识别（带版本/SHA，支持无 `.git` ZIP）；本地发布 tag / origin/master 可补充历史识别。
未知来源或不同目录的旧注册需人工核对，不猜测迁移。新用户必须在 CLI/TUI 的钩子管理入口或
Desktop「设置 → 编码 → 钩子」审阅并授信；脚本内容更新不改变现有声明，新增声明需单独授信。

运行中的 API 不会因复制脚本或更新源码热替换。`status` 检测到所属运行记录的源码指纹变化时提示需
重启；旧记录/外部服务缺少指纹时报告未知。合适时机显式停止所属 runtime，再重连 Codex：

```bash
python3 scripts/codex_runtime.py stop --api-url http://127.0.0.1:8000 --runtime-dir "$HOME/.codex/ai-team-os/runtime"
```

`stop` 只停止身份核对成功的所属实例；复用的共享服务不属于安装器。`start` 是另一个前台入口，
通过原子绑定 socket 防止竞争启动两个 API。完整按需安装和并发安全启动当前仅支持 POSIX；本轮
实测平台为 macOS，不宣称 Windows 通过。

卸载仅摘除尾部注册；会移动第三方授信槽位的中间卸载直接拒绝。MCP 仅恢复仍等于安装值的字段；
用户新增/修改的配置和文件保留。卸载不停止 API，不删除 SQLite、会话、凭据或 Claude 文件。
`upgrade` 是显式升级入口：工作区含未提交或未跟踪文件时拒绝，然后依次执行 `git pull --ff-only`、
所选解释器的 `-m pip install -e .`、新源码中的 `update` 与 `status`；任一步失败即停止，
不重启运行中的 API。`upgrade --dry-run` 只检查工作区并显示命令。`install/update` 本身不拉取或安装依赖。
升级只允许在回执 `repo_root` 指向的同一 Git 顶层目录执行，当前分支必须是 `master` 或回执已记录的
`source_branch`；旧回执未记录分支时只允许 `master`。缺少有效回执、不同源码树（包括另一 worktree）、
detached HEAD 或不允许的分支都会在拉取/安装前拒绝，并提示在回执安装树核对后运行的命令。
拉取结束、执行 pip 前会再次核对来源和分支；不会自动切分支，也没有绕过开关。

`update/upgrade/status` 默认沿用回执里的解释器、hooks-only 模式、API 地址与 runtime 目录；
API 地址不受当前进程的 `AITEAM_API_URL` 覆盖。旧回执没有模式字段时，以是否记录过 MCP 安装推断。
已有 stdio 用户首次可指定 `update --hooks-only`，只更新 Hook 并逐字节保留 MCP 配置；
`install --hooks-only` 和 `status --hooks-only` 同样可用。完整模式不会自动把 stdio 改为 HTTP。
显式 `--python`、`--api-url`、`--runtime-dir`、`--hooks-only/--no-hooks-only` 可覆盖默认选择，
但已有 MCP 地址或用户改过的 helper 不会被静默替换。

### 对话授权更新接口

`update --dry-run --json` 输出稳定 JSON：`targets` 逐项包含绝对 `path`、`action`（`create/write`）、
`before_sha256`、`after_sha256`、`summary`、可选来源 `source`；`baseline` 包含源码树、分支、提交、版本和
绑定全部相关文件及有效参数的 `state_sha256`；`options` 列出模式、解释器、地址与 runtime 目录。
预览不改安装时间，不创建备份或源码 `__pycache__`。只有实际字节变化的文件出现在写清单里。
安装回执 `.aiteam-codex-install.json` 的 `aiteam_version` 与预览 `baseline.version` 共用本次源码基线，
只来自目标树 `pyproject.toml` 的 `project.version`；未知时为空，不从当前已加载的 Python 包推断。
安装和更新都会持久化此字段，供 E15 检测器比较两侧已安装版本。
同一回执的 `source_branch` 记录本次源码基线分支，供后续 `upgrade` 核对已确认的安装来源。
从分发清单退役的已安装文件转入 `retired_files`，`retired_sha256` 保留最后已知安装摘要，不用当前用户
改过的字节替代。后续更新继续保留这些记录，即使文件已不在；重新进入分发清单才移回 `files/sha256`。
普通更新和确认预览后的更新均不删除退役脚本、不删除或移动旧 handler；原 `hooks.json` 仍是注册声明
证据。预览会明确提醒另行核查这些残留，E13 继续读取退役记录和当前文件/声明，更新成功不等于已清理。

API 服务可加载 `scripts/codex_adapter.py` 并调用：

```python
preview_update(repo_root, codex_home, interpreter=None, *,
               api_url=None, runtime_dir=None, hooks_only=None) -> dict
apply_update(repo_root, codex_home, interpreter=None, *, expected_preview,
             api_url=None, runtime_dir=None, hooks_only=None) -> dict
```

普通 `install/update` 拒绝覆盖用户改过的脚本。E13 对话更新的预览会明确标注这些文件及覆盖警告，
并明确提示普通 `update` 仍会拒绝，需经确认预览的应用流程。
只有携带确切 `expected_preview` 的应用才允许覆盖预览里的修改。MCP helper 命令、地址的保护仍保留。
`apply_update` 重新计算完整预览；源文件、目标文件、基线或参数发生漂移即拒绝。通过后对每个被覆盖文件
创建 `.bak-aiteam-<UTC时间>`，执行完整事务并校验写后摘要，失败恢复原字节。返回与预览相同的文件清单，
每项增加实际 `backup` 路径（新文件为空）。CLI 等价入口为
`update --expected-preview <预览JSON文件> --json`，也接受 `--expected-preview -` 从 stdin 读取。

这个预览参数只防漂移；对话授权仍由 `config_change` 负责：用进程内 HMAC 和 10 分钟有效期绑定预览，
验证 `user_quote` 后调用完整事务接口，并用返回的备份路径落 `decision.user_config_write` 事件。
不要把一次更新拆成多个逐文件回调，也不要将预览 JSON 当成授权 token。

原生隔离验收覆盖 116 工具发现、实际 MCP 调用、更新后新会话重连及卸载后不再加载 OS MCP。
Hook 的完整副本和新 Python 子进程执行已验；宿主首次授信后的自动 Hook → API → Dashboard
观测链尚需另行验收，不能由 MCP 成功推定。

### 本次未读提示的准备与交付

本次按用户任务，在同一开发批次准备 `channel_unread_codex.py` 与新增注册，仓内参照清单和锁同步再生；
这不改变两步发布纪律。用户交付依次进行：

1. 先交付脚本，不改已有注册声明。
2. 再于尾部追加 `UserPromptSubmit` 观察型 handler，3 秒超时，`additionalContextLimit=0`。
   当前 Codex 运行面保留 8 条基础注册，追加后共 9 条；Claude 专属的 workflow reminder、
   review link 和 meeting writeback 不进入 Codex 清单，避免跨宿主调用未适配入口。

用户层只生成待审阅候选，由用户应用；不得用仓内参照清单覆写整份用户配置，不得重排其他注册。
本目录的占位命令默认带 reader 参数 `leader-codex`。需要显式绑定项目时，候选命令使用
`channel_unread_codex.py leader-codex <project_id>`，其中第二个 argv 是实际项目 ID，不是目录名。
同级 worktree 无法按 cwd 解析项目时必须用此绑定；不能把解析失败当成零未读，也不能猜项目。
候选中 reader/project_id 都是注册声明的一部分，用户须审阅新增项的最终绝对路径和参数后授信。

该脚本是在用户提交提示时运行的按需通知，不是空闲唤醒器，不启动定时器或后台守护进程。
看到提示后按其中的项目与 reader 读取消息，再以实际已读消息时间显式 ACK；提示本身不移动水位。

## 版本常量

`CODEX_MIN_VERSION` 与 `CODEX_KNOWN_UPPER_VERSION` 是**全仓唯一**的版本字面量落点。
上界是滚动值，每次实测即刷新；同机多核的版本跨度是快照不是常量，**不得写死**。
版本真值只认 `session_meta.cli_version` 与 `state_5.threads.cli_version` 两个来源，
**禁止**用宿主命令行的版本子命令回填（它报的是 PATH 上的独立二进制，不等于承载内核），
也禁止采信模型自报版本；来源不对即落降级码 `DEGRADED_VERSION_SOURCE_UNTRUSTED`。
I-CDX-R8 对这三件事各有一条静态断言。
