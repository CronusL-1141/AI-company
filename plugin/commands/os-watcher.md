---
name: os-watcher
description: 开关「watcher 未武装」的提醒与收工拦截（管的是提醒本身，不启停 watcher）。无参数=查状态，off=静默，on=恢复，toggle=切换
allowed-tools: Bash
---

!`F="$HOME/.claude/data/ai-team-os/arm-hint.off"; mkdir -p "$(dirname "$F")" 2>/dev/null; case "$(printf '%s' "$ARGUMENTS" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')" in off|mute|close|关|静默) touch "$F";; on|open|开|恢复) rm -f "$F";; status|state|查|"") if [ -e "$F" ]; then echo "当前：已静默（不提醒、收工不拦）"; else echo "当前：守卫开着（未武装时每轮提醒，有活在飞时拦收工）"; fi; exit 0;; *) if [ -e "$F" ]; then rm -f "$F"; else touch "$F"; fi;; esac; if [ -e "$F" ]; then echo "已静默 — 不再每轮提醒，有活在飞时收工也不再拦你"; else echo "已开启 — 未武装时每轮提醒，且有活在飞时会拦一次收工"; fi`

上面一行是命令的执行结果。**把它原样转述给用户，一句话结束，不要再跑任何工具、不要复查文件、不要解释机制。**

（如果上面显示的是 `!` 加一段脚本原文而不是执行结果，说明这个环境没开 inline shell
execution —— 那就改用 Bash 工具执行同一段逻辑，再转述结果。）

## 先分清两件事

**本命令不启停 watcher 本身。** 它管的是「提醒你 watcher 没武装」这件事。

| 想做的事 | 用什么 |
| --- | --- |
| 真的武装一个 watcher | `bash scripts/os-watch.sh <session_id> <team_id> <reader> &` |
| 让它别再提醒/别再拦我 | `/os-watcher off` ← 本命令 |

`/os-watcher off` **不会**让已武装的 watcher 停掉，也**不会**替你武装一个。

## 这个开关管什么

`turn_end_guard.py` 的**待命守卫**，两件事由同一个开关管（2026-09-17 用户裁定合并）：

1. **每轮提醒** — UserPromptSubmit 注入的那句「事件 watcher 未武装…」
2. **收工拦截** — Stop 时若「有活在飞 + watcher 未武装」，拦一次逼你先武装（最多连拦
   `MAX_BLOCKS` 次）

合并的理由：只关嘴不关手等于没关。提示静默了却照样拦停，用户仍要逐次手动确认。

**静默的代价写在明处**：关掉之后有活在飞也能直接收工，活干完没人叫醒你。
**结果不会丢**——任务台账、事件、子 agent 产出照常落库，只是要等你下次开口才看到。

放行时走的是 `hint_muted` 分支，不与其它 allow 混淆：静默是你的选择，不是系统判定无风险，
两者在日志里必须分得开。

## 状态存哪

`~/.claude/data/ai-team-os/arm-hint.off`，**文件存在即静默**。随时可切，不用重启会话
（用文件而不是环境变量正是为此：改 env 要重启才生效）。

读不到这个文件时一律当作"没关"——开关本身出故障要回到有保护的那一侧。

## 关了之后想恢复叫醒能力

武装一个 watcher 即可，它和本开关互不影响：

```bash
bash scripts/os-watch.sh <session_id> <team_id> <你的 reader 标识> &
```

watcher 武装后，守卫本来也不会拦你（`watcher_armed` 分支放行），提醒也不会出现。
