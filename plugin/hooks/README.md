# AI Team OS — Hooks 迁移说明

## Phase 1（当前）：共存模式

Plugin `hooks.json` 与全局 `~/.claude/settings.json` 中的 hooks 配置共存。

- **全局 hooks**（`.claude/hooks/send_event.py`）：已有用户不受影响
- **Plugin hooks**（`plugin/hooks/send_event.py`）：Plugin 安装后自动生效

两套配置指向各自的脚本，逻辑相同，不会冲突。

## Phase 2（未来）：Plugin 接管

Plugin hooks 完全接管后，可移除全局 settings.json 中的 OS 相关 hooks：
1. 编辑 `~/.claude/settings.json`
2. 删除 hooks 中 `send_event.py` 相关条目（保留其他第三方 hooks）
3. Plugin hooks.json 自动提供所有事件覆盖

## 差异对比

| 项目 | 全局 settings.json | Plugin hooks.json |
|------|-------------------|-------------------|
| 路径 | `.claude/hooks/send_event.py` | `plugin/hooks/send_event.py` |
| timeout | 无（默认5s） | 2000ms |
| PreToolUse matcher | `*` | `Agent\|Bash\|Edit\|Write` |
| session_bootstrap | 无 | 有（API健康检测） |
| payload大小保护 | 仅字段级截断 | 字段级截断 + 整体32KB上限 |

## 第三方 Hooks 共存

全局 `settings.json` 中可能包含第三方 hooks（如 `observe.ps1`），它们与 Plugin hooks 互不干扰：
- 第三方 hooks 由全局 settings.json 管理，Plugin 不会触碰
- Plugin hooks 使用独立的 `hooks.json`，仅包含 AI Team OS 事件
- CC 会按顺序执行同一事件下的所有 hooks，不存在覆盖冲突

## API 不可用时的行为

- `send_event.py`: HTTP 请求 timeout 1.5s，失败后输出到 stderr，不阻塞 CC
- `session_bootstrap.py`: API 检测 timeout 1s，不可达时 stderr 提示运行 `/os-up`
- API 重启期间：hook 事件会丢失（fire-and-forget），重启后自动恢复
