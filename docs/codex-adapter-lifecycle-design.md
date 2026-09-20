# Codex 适配器生命周期设计

## 本批边界

本批交付源码 checkout 的完整 Codex MCP + Hook 安装、更新、状态检查、安全卸载与 HTTP 服务启动。下载、依赖安装、远端版本更新和 Windows 实机验收不属于已完成功能。Claude 配置、凭据与共享数据库不由适配器安装器改写。

## 安装及更新

`python3 scripts/codex_adapter.py install --api-url http://127.0.0.1:<端口>` 首次配置 MCP、连接 helper 及七个 Hook 运行文件，包括 `hook_core.py`；`update` 复用已有 API URL。目录优先级为 `--codex-home`、`CODEX_HOME`、用户默认目录。现有 observer handler 保留命令、参数、timeout、matcher 与索引；仅缺失 handler 在事件尾部追加。旧路径不猜测迁移，由 status 诊断。代码更新与注册变更分开报告，注册变更需重新授信。

拒绝写入 symlink；写前保留带唯一后缀的注册备份，安装文件、元数据与注册组成一次可回滚写操作。元数据保存安装文件摘要。用户在 observer 目录添加的文件不属于安装器；已安装文件存在用户修改时更新拒绝覆盖，卸载保留。

## 安全卸载

`uninstall` 默认预览，`--apply` 才执行。仅从 handler/group 尾部移除己方声明，保留第三方声明和所有剩余索引。若移除中间声明会移动第三方授信槽位，整次卸载拒绝修改并给出原因；未经宿主 schema 验证不制造空组占位。仅删除摘要匹配安装记录的文件；没有安装记录的卸载以当前源码摘要保守核对，未知或修改文件保留。已安装 MCP 字段按元信息恢复原值，后续用户修改的字段保留；认证、会话与数据库不删除。

## HTTP 服务恢复

`start` 是用户显式执行的前台命令，没有 cron、常驻调度器、隐式自启动和端口抢占。URL 优先级为 `--api-url`、`AITEAM_API_URL`、所选 Codex 配置的 HTTP MCP URL；缺少来源即给配置提示，不猜端口。启动前使用无代理 readiness 探测验证服务身份及 connection-cwd-v1 能力；已就绪则复用返回，端口被不兼容服务占用则拒绝启动。新服务前台运行，可用 Ctrl-C 正常关闭；POSIX 在应用 lifespan 前原子 bind/listen，再把 FD 交给 uvicorn，竞争失败者不启动应用或监控；失败不杀已有实例。Windows start 尚未支持。

`status` 另列 MCP URL、helper 可执行文件/脚本路径缺失和 readiness 结果；只输出白名单字段、不读取或显示凭据。helper 默认只生成连接头；显式 configure 后可通过 runtime-script 连接时按需确保服务就绪，详见 docs/codex-runtime-startup-design.md。未选择按需模式时，MCP 重新连接前仍需先启动服务。

## 验收

临时 CODEX_HOME 验证完整运行依赖、自定义 reader/project 参数、混合第三方组索引、修改文件保护、失败回滚。隔离 HOME/临时 DB/随机端口运行真实 HTTP initialize，验证重复 start 复用、端口冲突和 helper readiness。原生 Codex 临时会话验证完整新装后的 116 工具发现、实际 health/project_list 调用、跨请求项目读取、更新后重连和卸载后不再加载 OS MCP，均不发送模型 turn。Hook 仅验证完整安装及新 Python 子进程执行；未填充信任记录，尚未证明宿主授信后自动触发、事件持久化及 Dashboard 链路。首次使用者仍须在 CLI/TUI 或 Desktop 钩子界面审阅授信。

## 2026-09 发布生命周期验收补齐

完整安装/更新在显式 `--api-url`（或已有本机 MCP URL / `AITEAM_API_URL`）下，同时生成 HTTP MCP 配置、独立连接头 helper 与 hooks。首次安装不猜端口；安装动作只写配置，不启动 API。helper 连接时使用独立按需 runtime，运行目录默认位于所选 Codex home。系统 Python 与源码 checkout 是前置依赖，Windows 按需模式明确拒绝。

同一事务覆盖配置、helper、hooks 与安装元信息；预检先检查全部路径、TOML 语义和用户修改，写入失败恢复全部原字节。配置按 TOML 表定点修改且解析后核对，保留其它 MCP、账号、模型、第三方 hooks 和现有自定义参数。已有 OS MCP 地址与显式目标不同则拒绝；不把其它服务重定向。新增 MCP 字段记录原值与所写值，卸载仅恢复仍等于安装值的字段，用户后续修改保留。

旧安装没有元信息时，以随包发布且注明版本/SHA 的 legacy-installed-hashes.json，以及本地 Git 发布 tag 和 origin/master 的同路径 blob 摘要识别官方副本；只比当前文件会误拒绝真实升级。未知摘要或旧目录保持保守拒绝，不猜测迁移。测试从 v1.13.1 / origin/master 的真实发布树提取运行文件与清单，不能用当前副本伪装旧版。

更新 hook 副本会在下一次 hook 子进程启动生效；已载入 API 模块的进程不会热替换。更新结束明确提示在合适时机停止所拥有 runtime 并重新连接，安装器不停止共享服务。验收必须跨进程检查新装 MCP 116 工具发现与真实调用、旧版迁移、失败回滚、幂等以及卸载后数据/认证/第三方配置保留。原生 Codex 新会话不发送模型 turn；hook 授信仍由用户宿主管理界面完成，不自动填写信任记录。

`--hooks-only` 保留此前单独管理 Hook 的路径：install/update/status 不读取或修改 MCP 配置，已有 stdio 用户无需改用 HTTP 即可升级副本。完整模式不会自动改换 stdio 连接。发布 ZIP 的无 `.git` 新装与 v1.13.1 无元信息升级单独验收，内置旧摘要逐项对真实发布 blob 校验；Git 仅用于补充历史识别，不是正常安装/更新的必需运行依赖。
