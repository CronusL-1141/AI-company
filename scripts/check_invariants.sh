#!/usr/bin/env bash
# 红线不变量检查 — 把靠记忆维护的红线做成可执行检查。
# 每条检查对应一个真实踩过的事故（docs/knowledge-layer-design.md P0）。
# 用法: bash scripts/check_invariants.sh   （仓库根目录执行；CI 与本地通用）
# 退出码: 0=全过（警告不拦）, 1=有违规

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
FAIL=0
warn() { printf '⚠️  [%s] %s\n' "$1" "$2"; }
fail() { printf '❌ [%s] %s\n' "$1" "$2"; FAIL=1; }
ok()   { printf '✅ [%s] %s\n' "$1" "$2"; }

# ── I1: hook 双副本同步（事故: 715acc8 跨项目守卫只存在于从不分发的 src 副本）──
I1_BAD=""
for f in plugin/hooks/*.py; do
  base="$(basename "$f")"
  twin="src/aiteam/hooks/$base"
  if [ -f "$twin" ] && ! diff -q "$f" "$twin" >/dev/null 2>&1; then
    I1_BAD="$I1_BAD $base"
  fi
done
if [ -n "$I1_BAD" ]; then
  fail I1 "hook 双副本内容漂移:$I1_BAD —— plugin/hooks 与 src/aiteam/hooks 同名文件必须逐字节一致"
else
  ok I1 "hook 双副本同步"
fi

# ── I1b: 遗留 send_event 副本禁令（M27: 根 hooks/ 与 .claude/hooks/ 死副本曾漂移达 79 行）──
I1B_BAD=""
[ -f hooks/send_event.py ] && I1B_BAD="$I1B_BAD hooks/send_event.py"
[ -f .claude/hooks/send_event.py ] && I1B_BAD="$I1B_BAD .claude/hooks/send_event.py"
if [ -n "$I1B_BAD" ]; then
  fail I1b "遗留 send_event 副本死灰复燃:$I1B_BAD —— 真相源只有 plugin/hooks/send_event.py（src/aiteam/hooks 镜像由 I1 保证）"
else
  ok I1b "无遗留 send_event 副本"
fi

# ── I2: 版本五处锁步（事故: 7be8cd8 之前 9 处发散 0.0.0–1.6.1）──
I2_OUT="$(python3 - <<'EOF'
import json, re, sys
vals = {}
vals['pyproject'] = re.search(r'^version = "([^"]+)"', open('pyproject.toml').read(), re.M).group(1)
vals['__init__'] = re.search(r'__version__ = "([^"]+)"', open('src/aiteam/__init__.py').read()).group(1)
vals['plugin.json'] = json.load(open('plugin/.claude-plugin/plugin.json'))['version']
for tag, p in (('marketplace(plugin)', 'plugin/.claude-plugin/marketplace.json'),
               ('marketplace(root)', '.claude-plugin/marketplace.json')):
    d = json.load(open(p))
    plugins = d.get('plugins') or []
    vals[tag] = plugins[0].get('version') if plugins else d.get('version')
uniq = set(vals.values())
if len(uniq) != 1:
    print('MISMATCH ' + ', '.join(f'{k}={v}' for k, v in vals.items()))
    sys.exit(1)
print('VERSION ' + uniq.pop())
EOF
)" || true
case "$I2_OUT" in
  VERSION*) ok I2 "版本五处一致 (${I2_OUT#VERSION })" ;;
  *)        fail I2 "版本号漂移: ${I2_OUT#MISMATCH }" ;;
esac

# ── I3: 双 dist bundle 一致（事故: 发版日 plugin/dashboard-dist 滞后半天，人工才发现）──
if [ -d dashboard/dist/assets ] && [ -d plugin/dashboard-dist/assets ]; then
  A="$(ls dashboard/dist/assets/*.js 2>/dev/null | xargs -n1 basename 2>/dev/null | sort)"
  B="$(ls plugin/dashboard-dist/assets/*.js 2>/dev/null | xargs -n1 basename 2>/dev/null | sort)"
  if [ "$A" != "$B" ]; then
    fail I3 "dashboard/dist 与 plugin/dashboard-dist 的 JS bundle 不一致 —— 重新 cp -R dashboard/dist plugin/dashboard-dist"
  else
    ok I3 "双 dist bundle 一致"
  fi
else
  warn I3 "dist 目录缺失（未构建环境可忽略）"
fi

# ── I4: dist 不落后于前端源码（警告级——src 改动未必影响产物，但落后超 1 天值得看）──
I4_OUT="$(python3 - <<'EOF'
import os, sys
def newest(root, exts):
    latest = 0.0
    for dp, _dn, fns in os.walk(root):
        if 'node_modules' in dp or '/dist' in dp:
            continue
        for fn in fns:
            if fn.endswith(exts):
                try: latest = max(latest, os.path.getmtime(os.path.join(dp, fn)))
                except OSError: pass
    return latest
src = newest('dashboard/src', ('.ts', '.tsx', '.css'))
try:
    dist = max(os.path.getmtime(os.path.join('dashboard/dist/assets', f))
               for f in os.listdir('dashboard/dist/assets'))
except Exception:
    sys.exit(0)
lag_h = (src - dist) / 3600
print(f'{lag_h:.1f}')
EOF
)" || I4_OUT="0"
if python3 -c "import sys; sys.exit(0 if float('${I4_OUT:-0}') > 24 else 1)" 2>/dev/null; then
  warn I4 "dashboard/dist 落后前端源码 ${I4_OUT} 小时 —— 若改动涉及 UI 请重新构建"
else
  ok I4 "dist 时效正常"
fi

# ── I5: venv 禁令（血泪史: ae57984..e2d0fbb，四类进程共享依赖，venv 隔离已被否决）──
I5_HITS="$(grep -rnE '(-m venv|virtualenv|venv\.create|activate_this|\.venv/bin)' src/aiteam --include='*.py' 2>/dev/null | grep -v '^\s*#' | grep -vE '#.*(venv|virtualenv)' || true)"
if [ -n "$I5_HITS" ]; then
  fail I5 "src/ 内出现 venv 创建/激活代码（红线）:
$I5_HITS"
else
  ok I5 "无 venv 违规"
fi

# ── I6: README 数字机检（事故: 2026-07 审计发现 18 页/631+ 测试/30+ 生态工具三处数字腐烂，全部源于手工维护）──
I6_OUT="$(bash scripts/check_readme_numbers.sh 2>&1)"
if [ $? -eq 0 ]; then
  ok I6 "README 数字与实测一致（版本/MCP 工具/页面/测试，双语）"
else
  fail I6 "README 数字漂移 —— 双语 README 与代码实测不符:
$I6_OUT"
fi

echo
if [ "$FAIL" -eq 1 ]; then
  echo "结论: ❌ 存在红线违规，禁止提交/发布。修复后重跑 bash scripts/check_invariants.sh"
  exit 1
fi
echo "结论: ✅ 全部不变量通过"
