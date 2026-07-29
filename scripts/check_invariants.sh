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
# 双向集合比较 + 显式白名单：旧版只从 plugin 侧遍历并 `[ -f "$twin" ]` 跳过缺失，
# 于是「孪生副本不存在」和「只在 src 侧新增的文件」两类漂移全部静默漏检。
I1_OUT="$(python3 - <<'EOF'
import filecmp, os, sys

# 允许单侧存在的文件（各有明确理由，新增须在此显式登记）
PLUGIN_ONLY = {
    "auto_install.py",  # 插件自愈入口：从链外把链装起来，装进包内副本反而递归
}
SRC_ONLY = {
    "__init__.py",      # 包声明，不是 CC hook
}

def pys(d):
    return {f for f in os.listdir(d) if f.endswith(".py")}

plugin, src = "plugin/hooks", "src/aiteam/hooks"
p, s = pys(plugin), pys(src)
problems = []
for name in sorted((p - s) - PLUGIN_ONLY):
    problems.append(f"{name}: 只在 plugin/hooks 存在，缺 src/aiteam/hooks 孪生副本")
for name in sorted((s - p) - SRC_ONLY):
    problems.append(f"{name}: 只在 src/aiteam/hooks 存在（不会被分发，等于死代码）")
for name in sorted(p & s):
    if not filecmp.cmp(f"{plugin}/{name}", f"{src}/{name}", shallow=False):
        problems.append(f"{name}: 双副本内容漂移")
for name in sorted(PLUGIN_ONLY & s):
    problems.append(f"{name}: 白名单声明为 plugin 独有，却出现在 src/aiteam/hooks")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print(f"{len(p & s)} 对孪生副本逐字节一致（白名单豁免 {len(PLUGIN_ONLY | SRC_ONLY)} 个）")
EOF
)" && I1_OK=1 || I1_OK=0
if [ "$I1_OK" -eq 1 ]; then
  ok I1 "hook 双副本同步（${I1_OUT}）"
else
  fail I1 "hook 副本集合不匹配 —— plugin/hooks 与 src/aiteam/hooks 必须同名同内容:
$I1_OUT"
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
  ok I6 "README 数字与实测一致（版本/MCP 工具/页面/REST 端点/测试，双语）"
else
  fail I6 "README 数字漂移 —— 双语 README 与代码实测不符:
$I6_OUT"
fi

# ── I7: ruff lint 门禁（事故: 2026-07-21/22 两次 agent 交付代码未过 ruff 致公仓 CI Lint 红，人工验收清单靠不住，机器把关）──
if command -v ruff >/dev/null 2>&1; then
  I7_OUT="$(ruff check --quiet . 2>&1 || true)"
  if [ -z "$I7_OUT" ]; then
    ok I7 "ruff lint 全绿"
  else
    fail I7 "ruff lint 未过（公仓 CI 会红）:
$(echo "$I7_OUT" | head -20)"
  fi
else
  ok I7 "ruff 未安装，跳过（CI 仍会把关）"
fi

# ── I8: hook 注册面统一（事故: 2026-07-27 审计——源码安装与插件安装给出两个不同的 OS，
#        源码路径整整少 4 个事件，PreToolUse matcher 也对不上；README hook 数手工维护）──
I8_OUT="$(python3 scripts/check_hook_surface.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I8 "hook 注册面统一（install.py ↔ hooks.json ↔ 双语 README）"
else
  fail I8 "hook 注册面漂移 —— 两条安装路径会装出不同的 OS:
$I8_OUT"
fi

# ── I9: MCP 工具参数描述（事故: 2026-07-28 审计——10 个参数在线上 schema 里没有描述，
#        全部源于 docstring 里 `limit / offset: Pagination.` 这类一行写多参，源码看着
#        写全了、解析器却拆不开。tool search 时代没描述的参数等于搜不到也猜不对）──
I9_OUT="$(python3 scripts/check_tool_param_descriptions.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I9 "MCP 工具面描述完整（${I9_OUT#✅ MCP 工具面描述完整: }）"
else
  fail I9 "MCP 工具/参数缺 description —— 缺描述的参数在 tool search 里搜不到:
$I9_OUT"
fi

# ── I10: 表集合一致（事故: 2026-07-28 D0 取证——Mac 库 07-06 全新建库，Win 侧内容从未随迁，
#        而没有任何机检对照"代码认识的表"与"磁盘上的表"，静默丢表要等到有人去读才发现）──
I10_OUT="$(python3 scripts/check_schema_tables.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I10 "表集合一致（${I10_OUT##*✅ 表集合一致: }）"
  echo "$I10_OUT" | grep '^⚠️' || true
else
  fail I10 "ORM 声明的表在库中缺失 —— 建表失败或换机丢表:
$I10_OUT"
fi

# ── I11: 时钟约定统一（事故: 2026-07-28——核心域写本地墙钟、ecosystem 域写 UTC，
#        SQLite 落库把 offset 静默剥掉，两制的行长得一模一样，跨域比较偏 8 小时且
#        不抛异常。它活了几个月，一次审计抓到三处。双墙钟不是谁决定的，是一个模块
#        一个模块随手写出来的——没有机检，同样的事会再发生且同样没人看见）──
I11_OUT="$(python3 scripts/check_clock_convention.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I11 "时钟约定统一（${I11_OUT#✅ 时钟约定统一: }）"
else
  fail I11 "库里出现了第二个时钟 —— 这类错不抛异常，只会悄悄偏一个时区:
$I11_OUT"
fi

# ── I12: 用量呈现面量纲白名单（立项时那句"1.862 亿 token 已在库里"口径就是错的——
#        workflow_agents.tokens 是末轮上下文水位，与用量累加实测差 5~25 倍。混口径的
#        下一步就是混量纲：把 token 折成金额、折成工时、折成"相当于多少人天"。P1 定死
#        只以 token 表达，且用白名单而非禁用词表——黑名单漏一个写法就破防）──
I12_OUT="$(python3 scripts/check_usage_dimensions.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I12 "用量量纲白名单（${I12_OUT#✅ 量纲白名单通过: }）"
else
  fail I12 "用量呈现面出现白名单外的量纲 —— 只许 token 四层/次数/时长毫秒/百分比:
$I12_OUT"
fi

# ── I13: 覆盖率同屏红线（纪律① no-data≠zero 的呈现面形态：一个 token 数值脱离口径
#        与分母就没有意义。子 agent 用量的实测覆盖率是 11/2450 = 0.4%，此时报出一个
#        孤立总量就是局部冒充全貌。页面标注是软约束，所以这条钉在类型层——口径必须
#        写在字段旁边，聚合面必须与分母同层返回）──
I13_OUT="$(python3 scripts/check_usage_coverage.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I13 "覆盖率同屏红线（${I13_OUT##*✅ 覆盖率同屏红线通过: }）"
  echo "$I13_OUT" | grep '^⚠️' || true
else
  fail I13 "token 数值脱离口径/分母出现 —— 未归因不呈现就是局部冒充全貌:
$I13_OUT"
fi

# ── I14: 历史回采三条硬约束（设计 §6.4）。回采脚本一次改写两千余行生产数据，其中第一条
#        错了就无法挽回：workflow_agents.tokens 是 ctx_last 口径，回采产出的是 usage_sum，
#        实测差 5~25 倍——写进去等于把混口径永久固化进历史且事后不可分辨（R3）。所以这条
#        检查是行为式的：真建临时库、真跑一次 --apply、真比对禁改列的逐行 sha256 指纹，
#        文本扫描挡不住动态拼出来的 SQL，"跑完那一列有没有变"挡得住 ──
I14_OUT="$(python3 scripts/check_backfill_safety.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I14 "回采红线（${I14_OUT#✅ 回采红线通过: }）"
else
  fail I14 "历史回采硬约束失守 —— ctx_last 列被污染 / 覆盖率分窗合并 / 幂等或 dry-run 失效:
$I14_OUT"
fi

echo
if [ "$FAIL" -eq 1 ]; then
  echo "结论: ❌ 存在红线违规，禁止提交/发布。修复后重跑 bash scripts/check_invariants.sh"
  exit 1
fi
echo "结论: ✅ 全部不变量通过"
