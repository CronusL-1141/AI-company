# Codex 适配器生命周期设计

## 目标

让 Codex 用户从源码 checkout 或 release 包完成一套可重复的安装、更新、状态检查和卸载流程。Claude 的插件、`~/.claude` 配置、Hook 和数据不在本流程的写集内。

## 运行面

- 源码：`plugin/harness/codex/hooks/`、`surface.py` 与 `hooks.json`。
- 安装面：`$CODEX_HOME/hooks/ai-team-os-observer/`，默认 `$HOME/.codex`。
- 注册面：`$CODEX_HOME/hooks.json`。只维护命令路径落在 observer 目录内的组，保留其它用户 Hook 的顺序、内容和授信。
- 数据面：不删除或迁移 MCP、API、SQLite、会话和凭据；卸载只移除 Codex 适配器注册与复制文件。

## 命令契约

```bash
python3 scripts/codex_adapter.py install   # 首次安装或更新
python3 scripts/codex_adapter.py status    # 只读检查源码、安装副本和注册面
python3 scripts/codex_adapter.py uninstall # 预览/移除 Codex 适配器
```

所有写命令支持 `--dry-run`、`--codex-home PATH`、`--python PATH` 和 `--repo-root PATH`。写入前备份 `hooks.json`，采用临时文件加原子替换；失败不留下半份配置。

## 更新与授信

更新复制整套入口和配套模块，写入当前解释器与绝对路径，并原位替换已有 observer 注册组；不存在的事件追加到末尾，避免已有槽位整体滑移。脚本内容变化不要求重新授信；注册声明或 timeout 变化时输出明确的重新授信提示。更新后必须重新启动或重载 Codex，才能让已运行进程读取新文件。

## 卸载边界

卸载只删除本适配器拥有的注册组和 observer 目录；不删除第三方 Hook、不改 Claude、不停 API、不删除共享数据库。`--dry-run` 默认用于审阅将要删除的路径。
