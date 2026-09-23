# Codex 核心工具目录与现有角色适配

日期：2026-09-21。范围：仅 Codex harness、安装器及测试；共享 MCP、Claude 配置和 Hook 不改。

## 本批交付

1. 沿用已注册 SessionStart/SubagentStart，注入有预算的名称＋一句用途目录；不复制 schema，不新增 Hook 注册或授信槽。
2. 主会话目录覆盖现有 default＋links；子 Agent 目录仅列查询工具，任务记账仅在显式任务授权下提示。
3. 目录是发现提示，不是权限边界。参数通过当前宿主工具发现取得。已知禁用的工具不列入目录；无法解析配置时不猜测为全开。
4. 用户否决新增角色；已撤回 `aiteam-reader`、`aiteam-worker` 及其安装器配套逻辑。本机副本已验证归属并备份移除。
5. 工具分层应沿用现有default等角色，不更改主会话工具面、共享MCP或Claude。原生工具限制实测未生效前，不保留看似有效的重复连接配置。

## 实现约束

- 核心目录是 Codex 专属的短描述，不覆盖共享工具 docstring；中英双语，最多 32 条，固定字符预算。
- 只读取 Codex home 与当前仓库的配置，保守应用服务器 enabled/名单过滤；额外角色配置在身份可明确解析时应用。非完整原生合并器，目录须注明以实际工具目录为准。
- 子 Agent 提示不再依赖 task_id/project_id 才存在；任务绑定仍单独核验，不能因目录加载新增记账授权。
- 不调用工具列表统计端点，不新增后台线程或模型调用，不重启服务。
- 本批在原生启动/角色运行证据取得前，分别报告源码/安装/真实会话三种状态。

## 验收

- 模拟健康/失败、禁用服务器、单工具allow/deny、配置损坏、子角色及中英文返回；不会回显凭据或参数 schema。
- 接入测试验证健康提示与更新提醒保留，SubagentStart无绑定也得到安全查询目录，有绑定才有记账提示。
- 生命周期验证Hook升级幂等及第三方配置保持；不安装新角色。
- 所有 Codex 机检通过，Claude文件与共享MCP无新增差异；本机校验副本和配置，现有default过滤须用实际工具目录及无害拒绝调用验证。

## 本批实测结果

- 主入口已安装副本执行：32条名称简述，中文上下文合计1105字符，健康状态保留；未输出schema或业务全文。
- 原生新子Agent启动：真实观察到13条查询目录，未列os_restart_api；context_resolve成功。该默认角色实际仍可发现116工具，不能认定权限已受限。
- 新增角色方案已按用户要求撤回。随后在现有default配置中试验15项白名单：只有名单时报invalid transport；补齐连接后可启动，但新子会话仍有116工具，白名单外team_list实际调用成功。已恢复default原文件，避免留下无效且会漂移的连接副本。
- 115项定向/生命周期测试通过，1项原生opt-in跳过；I1–I21通过。Hook注册9槽未变化，无需重新授信。
- 最终本机原config.toml、hooks.json及default角色文件保持原样，CC与共享MCP未改。工具短目录保留，实际default工具面尚未收紧。未重启OS服务、未提交或发布。

## 最新待验收状态（用户要求新窗口验证）

用户指出旧主会话可能缓存工具面，要求重新准备配置并由其打开新窗口。已再次将完整OS连接字段及15项白名单写入现有subagent-context.toml，备份为同目录的.bak-aiteam-fresh-window-20260920T175019Z；主config.toml未改，窗口参数未改。此状态覆盖上文“最终default原样”的回滚记录。

白名单：context_resolve、unified_search、link_query、link_trace、memory_search、memory_list、report_list、report_read、task_status、task_list_project、task_memo_read、task_execution_trace、os_health_check、task_memo_add、report_save。

当前结论仅是旧root中新child未过滤，不能排除新root生效。待新主会话比较父/子真实工具目录并验证白名单外只读team_list；不调用管理工具、不绕过宿主走REST、不以配置文件存在作为成功证据。暂不再修改配置或重启共享服务。

## 新窗口结果与根因（覆盖上述待验收状态）

用户新开主会话后，父/子仍均为116工具；已核对本机记录中确为新父线程及default子角色，核心版本均0.155.1。

隔离原生控制实验：全新app-server、临时HOME、无害3工具MCP fixture、顶层enabled_tools仅允许context_resolve。实际目录只有1项，允许调用成功，名单外team_list被协议错误-32603明确拒绝。未发模型turn、未操作真实OS服务。证明本机顶层过滤可用。

本机二进制进一步确认：完整解析角色TOML后，只投影developer_instructions、model、model_reasoning_effort、model_reasoning_summary、model_verbosity、personality、service_tier、features、skills这9字段，再交给role_overrides::build_next_config；不包含mcp_servers。故不是反复开窗口能解决，也不是工具组写法错误。

已核对试验文件SHA后撤回无效角色连接副本，恢复原default文件，保留试验备份。工具短目录正常；实际角色工具过滤受当前宿主实现阻塞，不能用提示目录冒充权限。不得改主会话或共享MCP全局工具集来替代子agent隔离。
