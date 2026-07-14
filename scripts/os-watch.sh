#!/usr/bin/env bash
# scripts/os-watch.sh — 会话作用域事件 watcher（哑轮询器）。唤醒体系 v2，见
# docs/wake-loop-v2-design.md §7。
#
# 用法: bash scripts/os-watch.sh <session_id> [team_id]
#   （由 Leader 在 ACTIVE 态用 run_in_background 起：... &）
#
# 语义:
#   - 轮询 GET /api/wake/actionable（bash 零 SQL，判据集中在 API）
#   - 良性信号自吸收（agent 还在跑/run 未终态 → 继续睡）
#   - actionable（有 agent 收工/run 终态/新 memo）才退出，依赖 harness"后台任务
#     退出即重新调起模型"唤醒 Leader（batch0 实测机制存在）
#   - 1h 硬超时防僵尸；会话作用域（随会话进程消亡），绝非常驻件
#   - 运行期维护 wake-state/<sid>.armed（供 turn-end guard 判"watcher 已武装"），
#     退出即清除（trap）——guard 与 watcher 各占独立文件，无读改写竞态
#
# 退出码: 0=actionable命中 / 2=API不可达 / 3=硬超时
set -uo pipefail

SID="${1:?usage: os-watch.sh <session_id> [team_id]}"
TID="${2:-}"
POLL="${OS_WATCH_POLL:-8}"            # 轮询间隔秒
MAX_LIFETIME="${OS_WATCH_MAX:-3600}"  # 硬超时 1h，防僵尸

STATE_DIR="$HOME/.claude/data/ai-team-os/wake-state"
SAFE_SID="$(printf '%s' "$SID" | tr -c 'A-Za-z0-9._-' '_')"
ARMED_FILE="$STATE_DIR/${SAFE_SID}.armed"
PORT="$(cat "$HOME/.claude/data/ai-team-os/api_port.txt" 2>/dev/null || echo 8000)"
BASE="${AITEAM_API_URL:-http://localhost:${PORT}}"
mkdir -p "$STATE_DIR"

# 时间口径: 本地(匹配 DB naive-local datetime.now())，绝不用 -u UTC
SINCE="$(date +%Y-%m-%dT%H:%M:%S)"
START="$(date +%s)"

cleanup() { rm -f "$ARMED_FILE"; }
# EXIT 只清理；INT/TERM 清理并退出（否则 trap 后循环会继续，进程不随信号消亡）。
trap cleanup EXIT
trap 'cleanup; exit 143' INT TERM

# 心跳武装标记 = now + 2*poll（睡眠间隔内不过期；watcher 意外死亡则 ~2*poll 后自动失效）
arm() { echo "$(( $(date +%s) + 2 * POLL ))" > "$ARMED_FILE"; }

QS="session_id=${SID}"
[ -n "$TID" ] && QS="${QS}&team_id=${TID}"

while :; do
  arm
  if (( $(date +%s) - START >= MAX_LIFETIME )); then
    echo "WATCHER_TIMEOUT 达最大存活 ${MAX_LIFETIME}s，退出请 Leader 复核是否仍有活在飞"
    exit 3
  fi
  if ! RESP="$(curl -fsS --max-time 3 "${BASE}/api/wake/actionable?${QS}&since=${SINCE}" 2>/dev/null)"; then
    echo "WATCHER_API_UNREACHABLE 端点不可达，退出交由 /loop 兜底"
    exit 2
  fi
  if printf '%s' "$RESP" | grep -q '"actionable"[[:space:]]*:[[:space:]]*true'; then
    echo "ACTIONABLE ${RESP}"
    exit 0
  fi
  # 良性：滚动 watermark（单调水位，同一事件不重复报），继续睡
  NEW_SINCE="$(printf '%s' "$RESP" | sed -n 's/.*"watermark"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  [ -n "$NEW_SINCE" ] && SINCE="$NEW_SINCE"
  echo "STATUS benign next=${POLL}s"
  sleep "$POLL"
done
