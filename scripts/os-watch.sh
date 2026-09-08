#!/usr/bin/env bash
# scripts/os-watch.sh — 会话作用域事件 watcher（哑轮询器）。唤醒体系 v2，见
# docs/wake-loop-v2-design.md §7。
#
# 用法: bash scripts/os-watch.sh <session_id> [team_id] [reader]
#   （由 Leader 在 ACTIVE 态用 run_in_background 起：... &）
#
# 语义:
#   - 轮询 GET /api/wake/actionable（bash 零 SQL，判据集中在 API）
#   - 良性信号自吸收（agent 还在跑/run 未终态 → 继续睡）
#   - actionable（有 agent 收工/run 终态/新 memo/信道有人点名你）才退出，依赖
#     harness"后台任务退出即重新调起模型"唤醒 Leader（batch0 实测机制存在）
#   - reader 给了才把信道点名算作唤醒信号。它是角色标识（leader-cc / leader-codex），
#     不是 session_id。这一项让"对端发消息时把你叫醒"成立——没有它，消息要躺到
#     下次有人跟你说话才被看见（实测躺过半小时）
#   - 1h 硬超时防僵尸；会话作用域（随会话进程消亡），绝非常驻件
#   - 运行期维护 wake-state/<sid>.armed（供 turn-end guard 判"watcher 已武装"），
#     退出即清除（trap）——guard 与 watcher 各占独立文件，无读改写竞态
#
# 退出码: 0=actionable命中 / 2=API不可达 / 3=硬超时
set -uo pipefail

SID="${1:?usage: os-watch.sh <session_id> [team_id] [reader]}"
TID="${2:-}"
RDR="${3:-}"
POLL="${OS_WATCH_POLL:-8}"             # 轮询间隔秒
# 存活上限从 1h 提到 12h（2026-09-08）。原来那个小时限是拿时间当"孤儿"的代理指标，
# 代价却压在功能可靠性上：Leader 每小时都得记得重新武装，忘一次就等于这个功能没装
# （实测忘过一次，是缔造者发现的）。而实测占用是 CPU 0.0% / 常驻约 3MB / 450 次本地
# 环回请求每小时——为这点开销牺牲"消息有人管"不划算。
# 孤儿改由下面的 PPID 检测直接判定，比时间精确；这一项退居最后兜底。
MAX_LIFETIME="${OS_WATCH_MAX:-43200}"
# 探测失败分两类判定，因为"忙"和"死"的正确反应相反：
#   连接被拒(curl 7)  = 服务真的没了 → 快报，别让故障闷着
#   超时(curl 28)     = 服务在忙     → 继续守，这正是最需要守望的时候
# 实测三次同型故障：本机跑全量测试、gh 上传发布正文时，3 秒预算够不着一次正常响应，
# 旧逻辑当场判"不可达"离岗。curl 早就把两者分开了，脚本跟着分开即可。
MAX_FAILS="${OS_WATCH_MAX_FAILS:-3}"          # 连接被拒几次判服务没了
MAX_BUSY="${OS_WATCH_MAX_BUSY:-40}"           # 超时几次才放弃（默认约 5 分钟 @8s）
CURL_TIMEOUT="${OS_WATCH_CURL_TIMEOUT:-8}"    # 单次请求预算，与轮询间隔同量级
FAILS=0
BUSY=0
# 启动时的父进程。会话消亡后本进程会被 PID 1 收养，这是"我成孤儿了"的直接证据，
# 不必靠时间去猜。会话作用域的承诺由它兑现——这也是本脚本仍不算常驻件的依据。
START_PPID="$PPID"

STATE_DIR="$HOME/.claude/data/ai-team-os/wake-state"
SAFE_SID="$(printf '%s' "$SID" | tr -c 'A-Za-z0-9._-' '_')"
ARMED_FILE="$STATE_DIR/${SAFE_SID}.armed"
PORT="$(cat "$HOME/.claude/data/ai-team-os/api_port.txt" 2>/dev/null || echo 8000)"
BASE="${AITEAM_API_URL:-http://localhost:${PORT}}"
mkdir -p "$STATE_DIR"

# 时间口径: UTC，且**带 +00:00 偏移**——全库统一 UTC 后水位串自描述，两端不再靠
# 各自的约定对齐。首个 SINCE 由本脚本生成，此后一律沿用 API 回传的 watermark。
SINCE="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
START="$(date +%s)"

cleanup() { rm -f "$ARMED_FILE"; }
# EXIT 只清理；INT/TERM 清理并退出（否则 trap 后循环会继续，进程不随信号消亡）。
trap cleanup EXIT
trap 'cleanup; exit 143' INT TERM

# 心跳武装标记 = now + 2*poll（睡眠间隔内不过期；watcher 意外死亡则 ~2*poll 后自动失效）
arm() { echo "$(( $(date +%s) + 2 * POLL ))" > "$ARMED_FILE"; }

QS="session_id=${SID}"
[ -n "$TID" ] && QS="${QS}&team_id=${TID}"
[ -n "$RDR" ] && QS="${QS}&reader=${RDR}"

# query string 里 '+' 是空格的编码。时间戳的 "+00:00" 不编码就会在服务端变成空格、
# 解析失败、退化成"不设下界"——于是每一轮都判 actionable，watcher 一起来就退出。
# 这条静默失效实测过：since 声称 2 秒前，API 却回报 1513 条新 memo。
enc_since() { printf '%s' "${1//+/%2B}"; }

while :; do
  arm
  # 孤儿检测优先于时间：父进程没了说明会话已消亡，继续守望没有意义，也没人能被唤醒。
  NOW_PPID="$(ps -o ppid= -p $$ 2>/dev/null | tr -d ' ')"
  if [ -n "$NOW_PPID" ] && [ "$NOW_PPID" != "$START_PPID" ] && [ "$NOW_PPID" = "1" ]; then
    echo "WATCHER_ORPHANED 父进程已退出（会话消亡），停止守望"
    exit 4
  fi
  if (( $(date +%s) - START >= MAX_LIFETIME )); then
    echo "WATCHER_TIMEOUT 达最大存活 ${MAX_LIFETIME}s，退出请 Leader 复核是否仍有活在飞"
    exit 3
  fi
  RESP="$(curl -fsS --max-time "$CURL_TIMEOUT" \
      "${BASE}/api/wake/actionable?${QS}&since=$(enc_since "$SINCE")" 2>/dev/null)"
  CURL_RC=$?
  if (( CURL_RC != 0 )); then
    # curl 28 = 超时。服务在忙不等于服务没了，而机器忙的时候恰恰最需要有人守着。
    # 这里宽容得多：默认 40 次（约 5 分钟 @8s），够扛完一次全量测试或一次发布上传。
    if (( CURL_RC == 28 )); then
      BUSY=$((BUSY + 1))
      if (( BUSY >= MAX_BUSY )); then
        echo "WATCHER_API_UNREACHABLE 连续 ${BUSY} 次请求超时（服务持续无响应），退出交由 /loop 兜底"
        exit 2
      fi
      echo "STATUS api_hiccup busy ${BUSY}/${MAX_BUSY} next=${POLL}s"
      sleep "$POLL"
      continue
    fi
    # 其余（尤其 7=连接被拒）当作服务真的没了：少数几次就退出，别让故障闷着。
    FAILS=$((FAILS + 1))
    if (( FAILS >= MAX_FAILS )); then
      echo "WATCHER_API_UNREACHABLE 连续 ${FAILS} 次探测失败（curl rc=${CURL_RC}），退出交由 /loop 兜底"
      exit 2
    fi
    echo "STATUS api_hiccup ${FAILS}/${MAX_FAILS} rc=${CURL_RC} next=${POLL}s"
    sleep "$POLL"
    continue
  fi
  FAILS=0
  BUSY=0
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
