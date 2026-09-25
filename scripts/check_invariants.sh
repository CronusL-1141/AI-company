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

# ── I1: hook 副本三方对钉（事故: 715acc8 跨项目守卫只存在于从不分发的 src 副本）──
# 双向集合比较 + 显式白名单：旧版只从 plugin 侧遍历并 `[ -f "$twin" ]` 跳过缺失，
# 于是「孪生副本不存在」和「只在 src 侧新增的文件」两类漂移全部静默漏检。
# 第三方 = Codex 适配器的 hook 目录。共用核心（hook_core.py）在三处各存一份且必须
# 逐字节相同——这是「一核两适配器」里「核只有一份」的机检形态。适配器自有的入口脚本
# 反过来禁止出现在两个 CC 目录里：同名文件会让「这份运行时文件属于哪个 harness」无从
# 判读，也会让上面那组逐字节断言失去意义。第三目录缺失只 warn 不红——它在适配器尚未
# 落地的机器与 CI 上本就不存在，红了就是假红。
I1_OUT="$(python3 - <<'EOF'
import filecmp, importlib.util, os, sys

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
codex_dir = "plugin/harness/codex/hooks"
surface_path = "plugin/harness/codex/surface.py"
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

# 入口从注册表派生，配套模块单独显式登记；模块不是 handler，不改变授信面。
codex_only = set()
codex_entries = set()
codex_support = set()
if os.path.isfile(surface_path):
    spec = importlib.util.spec_from_file_location("_codex_surface_i1", surface_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    codex_entries = set(getattr(mod, "CODEX_HOOK_SCRIPTS", ()))
    support = getattr(mod, "CODEX_SUPPORT_MODULES", ())
    if (not isinstance(support, tuple)
            or any(not isinstance(name, str) or os.path.basename(name) != name
                   or "\\" in name or not name.endswith(".py") for name in support)):
        problems.append("CODEX_SUPPORT_MODULES 必须是同目录 Python 文件名的 tuple")
    else:
        codex_support = set(support)
        if len(codex_support) != len(support):
            problems.append("CODEX_SUPPORT_MODULES 存在重复登记")
        if codex_support & codex_entries:
            problems.append("配套模块与 Hook 入口重复登记")
    codex_only = codex_entries | codex_support
for name in sorted(codex_only & (p | s)):
    where = " / ".join(d for d, files in ((plugin, p), (src, s)) if name in files)
    problems.append(f"{name}: 适配器私有文件名污染了 CC hook 目录（{where}）")

if os.path.isdir(codex_dir):
    c = pys(codex_dir)
    for name in sorted(codex_support):
        path = os.path.join(codex_dir, name)
        if not os.path.isfile(path) or os.path.islink(path):
            problems.append(f"{name}: 已登记配套模块缺失或不是独立普通文件")
    shared = sorted(c & p & s)
    for name in shared:
        if not filecmp.cmp(f"{plugin}/{name}", f"{codex_dir}/{name}", shallow=False):
            problems.append(f"{name}: 共用核心第三副本与 plugin/hooks 漂移")
    for name in sorted((c - p) - codex_only):
        problems.append(f"{name}: 只在 {codex_dir} 存在且未登记为入口或配套模块")
    for name in sorted((c & p) - s):
        problems.append(f"{name}: 在 CC 侧只有 plugin 一份，三方对钉不成立")
    third = (f"；Codex 面 {len(shared)} 个共用核心三方一致，"
             f"{len(codex_entries)} 个入口与 {len(codex_support)} 个配套模块未污染 CC 目录")
else:
    third = "；Codex 面 hooks 目录缺失，三方对钉跳过"

if problems:
    print("\n".join(problems))
    sys.exit(1)
head = "" if os.path.isdir(codex_dir) else "SKIPPED_CODEX "
print(f"{head}{len(p & s)} 对 CC 孪生副本逐字节一致"
      f"（白名单豁免 {len(PLUGIN_ONLY | SRC_ONLY)} 个）{third}")
EOF
)" && I1_OK=1 || I1_OK=0
if [ "$I1_OK" -eq 1 ]; then
  case "$I1_OUT" in
    SKIPPED_CODEX*) warn I1 "hook 副本三方对钉（${I1_OUT#SKIPPED_CODEX }）" ;;
    *)              ok   I1 "hook 副本三方对钉（${I1_OUT}）" ;;
  esac
else
  fail I1 "hook 副本集合不匹配 —— plugin/hooks 与 src/aiteam/hooks 必须同名同内容，
Codex 适配器目录只许放共用核心的逐字节副本、已登记入口与配套模块:
$I1_OUT"
fi

# ── I1b: 遗留 send_event 副本禁令（M27: 根 hooks/ 与 .claude/hooks/ 死副本曾漂移达 79 行）──
# 入口冻结 + hook_core 共用：CC 入口 send_event.py 的真相源只有 plugin/hooks 一份，
# 字节由 I1c 冻结；两 harness 共享的加工逻辑走 hook_core.py，由 I1 三方逐字节对钉。
# 三条一起把「复制一份改改」这条老路堵死——旧副本禁令挡树内死副本，入口冻结挡对唯一
# 真本的悄悄改写，共用核心则让本该共享的东西有个不必复制的去处。
I1B_BAD=""
[ -f hooks/send_event.py ] && I1B_BAD="$I1B_BAD hooks/send_event.py"
[ -f .claude/hooks/send_event.py ] && I1B_BAD="$I1B_BAD .claude/hooks/send_event.py"
if [ -n "$I1B_BAD" ]; then
  fail I1b "遗留 send_event 副本死灰复燃:$I1B_BAD —— 真相源只有 plugin/hooks/send_event.py\
（src/aiteam/hooks 镜像由 I1 保证，字节由 I1c 冻结，共享逻辑走 hook_core.py）"
else
  ok I1b "无遗留 send_event 副本"
fi

# ── I1c: hook 入口哈希冻结（「CC 入口原样不动、哈希不变可机检」的落点）──
# 字母后缀的子编号不占 I 号：⑥ 类锚的真相源正则是 `^# ── I[0-9]+:`，I1b/I1c 这类
# 后缀写法天然不计入，README 的不变量条数只数主编号。新增子编号沿用此写法。
# 冻结档 scripts/hook_entry_freeze.json 是单一真相源，与 CC 差分回放测试共读同一份。
# 解冻只能走核心 PR，且须同批重算差分 golden 并在 PR 里人审 golden diff。
I1C_OUT="$(python3 - <<'EOF'
import hashlib, json, os, sys

FREEZE = "scripts/hook_entry_freeze.json"
if not os.path.isfile(FREEZE):
    print(f"{FREEZE} 缺失 —— 入口冻结档是「入口未被改写」的唯一凭据，不得删除")
    sys.exit(1)
frozen = json.load(open(FREEZE, encoding="utf-8"))
if not frozen:
    print(f"{FREEZE} 为空 —— 空冻结档等于没有冻结")
    sys.exit(1)
problems = []
for path, expect in sorted(frozen.items()):
    if not os.path.isfile(path):
        problems.append(f"{path}: 冻结档登记的入口文件不存在")
        continue
    actual = hashlib.sha256(open(path, "rb").read()).hexdigest()
    if actual != expect:
        problems.append(f"{path}: sha256 {actual[:12]}… ≠ 冻结值 {expect[:12]}…")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print(f"{len(frozen)} 个入口与冻结档逐位相符")
EOF
)" && I1C_OK=1 || I1C_OK=0
if [ "$I1C_OK" -eq 1 ]; then
  ok I1c "hook 入口哈希冻结（${I1C_OUT}）"
else
  fail I1c "hook 入口字节变了 —— 解冻只能走核心 PR，且须同批重算 CC 差分 golden 并人审 diff:
$I1C_OUT"
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

# ── I3: 双 dist bundle 一致（事故: 发版日 plugin/dashboard-dist 滞后半天，人工才发现；
#     0914 反例: 主 checkout 本地 dist 反而比跟踪面旧两天，照旧提示直接拷贝会把新包盖成旧包——
#     脚本判不出哪边新，修法只能是先从源码重建再同步）──
if [ -d dashboard/dist/assets ] && [ -d plugin/dashboard-dist/assets ]; then
  A="$(ls dashboard/dist/assets/*.js 2>/dev/null | xargs -n1 basename 2>/dev/null | sort)"
  B="$(ls plugin/dashboard-dist/assets/*.js 2>/dev/null | xargs -n1 basename 2>/dev/null | sort)"
  if [ "$A" != "$B" ]; then
    fail I3 "dashboard/dist 与 plugin/dashboard-dist 的 JS bundle 不一致 —— 哪边新脚本判不出，别直接拷贝：先 (cd dashboard && npm run build)，再 rm -rf plugin/dashboard-dist && cp -R dashboard/dist plugin/dashboard-dist"
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
  ok I6 "README 数字与实测一致（版本/MCP 工具/页面/REST 端点/规则/测试，双语）"
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
  ok I8 "hook 注册面统一（install.py ↔ hooks.json ↔ 双语 README ↔ plugin.json）"
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
#        只以 token 表达，且用白名单而非禁用词表——黑名单漏一个写法就破防）。
#        多 harness 后本条多管一层：四层可加性要覆盖每个 harness × 每一层，其中"线上
#        有这个字段但没验证过它是真的"这一档必须显式标出来（wire_present_unverified），
#        不能与"确实可得"混成一格——空格与未标注的格子都算漂移 ──
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
#        写在字段旁边，聚合面必须与分母同层返回）。
#        多 harness 后覆盖率按 harness 与桶分列，并增一条活性断言：归因链断掉时表面
#        看不出异常，分子会安静地停在 0 而分母照常增长。活性判据必须两列同校验——单
#        列启发式实测误命中 291 行；结构性不可达的那一类进免检桶、不进分母；生产滚动
#        桶与夹具冻结桶同屏分列且禁止相加，生产桶呈现必带统计时点 ──
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
#        文本扫描挡不住动态拼出来的 SQL，"跑完那一列有没有变"挡得住。
#        回采器是两 harness 共用的，所以临时库里两种形状的行都要有：谓词一旦漏掉分级，
#        另一个 harness 的行会被当成同形状回采，写进去同样不可分辨、同样不可挽回 ──
I14_OUT="$(python3 scripts/check_backfill_safety.py 2>&1)"
if [ $? -eq 0 ]; then
  ok I14 "回采红线（${I14_OUT#✅ 回采红线通过: }）"
else
  fail I14 "历史回采硬约束失守 —— ctx_last 列被污染 / 覆盖率分窗合并 / 幂等或 dry-run 失效:
$I14_OUT"
fi

# ── I15: Codex hook 清单生成期 schema（清单写错了不会报错，只会安静地少一个 handler：
#        宿主对不认识的事件名一声不吭地跳过，对写错面的 matcher 也是——两者的表现形态
#        都是"什么都没发生"，与"这次确实没触发"长得一模一样。所以清单与注册表必须 1:1，
#        matcher 只许写 hook payload 面、禁止出现模型面带点全名，工具名映射键必须在主表
#        登记过，占位符成对、版本字面量只许住在两个常量里 ──
if [ -f scripts/check_codex_hook_surface.py ]; then
  I15_OUT="$(python3 scripts/check_codex_hook_surface.py 2>&1)"
  if [ $? -eq 0 ]; then
    ok I15 "Codex hook 清单 schema（${I15_OUT#\[OK\] I15/R8: }）"
  else
    fail I15 "Codex hook 清单与注册表不符 —— 写错的 handler 不会报错，只会永远不触发:
$I15_OUT"
  fi
else
  fail I15 "scripts/check_codex_hook_surface.py 缺失 —— I15 是清单唯一的生成期把关，删了等于没有"
fi

# ── I16: execpolicy rules 过宿主校验（rules 文件与宿主二进制都还没落地，本期只把机检
#        位子占住并如实报 warn。占位而不静默的理由：一条"暂时没有输入"的检查看得见，
#        一条根本不存在的检查看不见 ──
if [ -f scripts/check_codex_execpolicy.py ]; then
  I16_OUT="$(python3 scripts/check_codex_execpolicy.py 2>&1)"
  I16_RC=$?
  if [ "$I16_RC" -ne 0 ]; then
    fail I16 "宿主拒收 execpolicy rules:
$I16_OUT"
  else
    case "$I16_OUT" in
      \[WARN\]*) warn I16 "Codex execpolicy（${I16_OUT#\[WARN\] I16: }）" ;;
      *)         ok   I16 "Codex execpolicy（${I16_OUT#\[OK\] I16: }）" ;;
    esac
  fi
else
  fail I16 "scripts/check_codex_execpolicy.py 缺失 —— 位子被删掉就再没人记得这条该做"
fi

# ── I17: hook 授信锁与清单一致（宿主把授信记录钉在「清单路径 + 事件 + 组序号 + handler
#        序号」上，所以在中间插一条就会让它后面每一条静默失信——用户那边不会有任何提示，
#        只是从此不再触发。lock 只锁声明五元组、不锁脚本内容：换脚本不该要求重新授信，
#        锁了内容反而每次普通改码都喊狼来了 ──
if [ -f scripts/check_codex_trust_lock.py ]; then
  I17_OUT="$(python3 scripts/check_codex_trust_lock.py 2>&1)"
  if [ $? -eq 0 ]; then
    ok I17 "Codex hook 授信锁（${I17_OUT#\[OK\] I17: }）"
  else
    fail I17 "授信锁与清单不符 —— 发布前须预测失信清单，并在 Release notes 写明需重新授信:
$I17_OUT"
  fi
else
  fail I17 "scripts/check_codex_trust_lock.py 缺失 —— 没有它就无法预测哪几条会失信"
fi

# ── I17b: 授信槽位不得改嫁（I17 只比「锁 ↔ 本次清单」，看不见用户装的是哪一版，于是它
#        对跨版本的槽位复用是瞎的。宿主按位置记授信：中间摘掉或插入一条，同事件后面每条
#        都滑位并原样继承前任的授信——用户不会被重新询问，日志里也不会有一行。两种后果都
#        无声：没批准过的命令顶着旧授信跑，或批准过的从此不触发。基线是「上次发布面」，
#        发布时用 --advance 推进，推进那一刻正是写 Release notes 授信指引的时刻 ──
if [ -f scripts/check_codex_trust_drift.py ]; then
  I17B_OUT="$(python3 scripts/check_codex_trust_drift.py 2>&1)"
  if [ $? -eq 0 ]; then
    ok I17b "Codex 授信槽位（${I17B_OUT%%$'\n'*}）"
    echo "$I17B_OUT" | tail -n +2
  else
    fail I17b "授信槽位被复用 —— 旧授信会套到新命令上:
$I17B_OUT"
  fi
else
  fail I17b "scripts/check_codex_trust_drift.py 缺失 —— 跨版本的授信改嫁将重新变得不可见"
fi

# ── I18: AGENTS.md ≡ 标准头 + CLAUDE.md 共享段（CLAUDE.md 是正本，<!-- codex:end --> 之前
#        为两宿主共守的共享段、之后为 Claude Code 专属段；AGENTS.md 只取共享段。两份文件
#        必然发散——发散的那半边不会报错，只会让其中一个 harness 按过期规则干活。恒等式 +
#        体积闸 + 共享段无宿主专属词，压成一条机检；改 CLAUDE.md 的任何一批都必须同批重跑生成器 ──
if [ -f scripts/check_agents_md.py ]; then
  I18_OUT="$(python3 scripts/check_agents_md.py 2>&1)"
  if [ $? -eq 0 ]; then
    ok I18 "AGENTS.md 恒等（${I18_OUT#✅ I18: }）"
  else
    fail I18 "AGENTS.md 与 CLAUDE.md 共享段不等 / 超限 / 共享段含宿主专属词 —— 改了 CLAUDE.md 请同批跑 scripts/gen_agents_md.py:
$I18_OUT"
  fi
else
  fail I18 "scripts/check_agents_md.py 缺失 —— 两份规则文件失去唯一的对钉手段"
fi

# ── I19: Codex 夹具与 golden 绑定（夹具是采集器唯一的回归标尺，而标尺自己会烂：采集器
#        改了 golden 不重算、golden 改了没人复算、README 里的文件数与磁盘对不上，三种
#        腐烂都不会让任何测试变红。本条是薄壳——判据全在 pytest 那份实现里，这里只负责
#        让它进机检链路，并在 pytest 不可用时红而不是跳过 ──
if [ -f scripts/check_codex_fixtures.py ]; then
  I19_OUT="$(python3 scripts/check_codex_fixtures.py 2>&1)"
  if [ $? -eq 0 ]; then
    ok I19 "Codex 夹具接 golden（${I19_OUT#✅ I19 通过: }）"
  else
    fail I19 "夹具与 golden 失联 —— 采集器改了没重算，或 golden 改了没复算:
$I19_OUT"
  fi
else
  fail I19 "scripts/check_codex_fixtures.py 缺失 —— 夹具会重新掉出机检链路"
fi

# ── I20: 适配器安装隔离（「一核两适配器」是个关于「够不着」的断言，而够得着与够不着在
#        代码评审里长得差不多。三条静态断言把它变成机检：适配器不认识 CC 装在哪、CC 安装
#        器的取源集合没有长出第六项、两侧入口脚本零重名。运行时那一半（装两次结果逐字节
#        相同、CC 配置零改动）随安装器落地，本期只做静态半边 ──
if [ -f scripts/check_codex_isolation.py ]; then
  I20_OUT="$(python3 scripts/check_codex_isolation.py 2>&1)"
  if [ $? -eq 0 ]; then
    ok I20 "适配器安装隔离（${I20_OUT#\[OK\] I20: }）"
  else
    fail I20 "适配器越界 —— 装 CC 会捎上 Codex 文件，或适配器伸手进了 CC 安装路径:
$I20_OUT"
  fi
else
  fail I20 "scripts/check_codex_isolation.py 缺失 —— 单向边界失去唯一的机检背书"
fi

# ── I21: 两侧信道读者标识互斥（未读水位按 reader 记账，两侧若共用同一个 reader，一侧读完
#        推进水位会连带清掉另一侧的徽章——发给它的消息永远不会被提示，而两侧各自的测试
#        全都照常通过。这条失效完全无声，只有机检拦得住 ──
if [ -f scripts/check_reader_identity.py ]; then
  I21_OUT="$(python3 scripts/check_reader_identity.py 2>&1)"
  if [ $? -eq 0 ]; then
    ok I21 "信道读者身份（${I21_OUT#\[OK\] I21: }）"
  else
    fail I21 "读者身份冲突 —— 一侧清零会连带吃掉另一侧的未读:
$I21_OUT"
  fi
else
  fail I21 "scripts/check_reader_identity.py 缺失 —— 两侧共用 reader 将无人拦截"
fi

# ── I22: 用户提示单出口（hook 直接写 systemMessage 或面向用户的措辞，会绕过前缀、160 列、
#        着色门槛、宿主字段白名单与每会话预算——提示噪声正是这样一条条长出来的；
#        另查源码安装把 hook 依赖的 user_notice.py 一并复制，漏了它，所有用户行静默消失）──
if [ -f scripts/check_user_notice_exit.py ]; then
  I22_OUT="$(python3 scripts/check_user_notice_exit.py 2>&1)"
  if [ $? -eq 0 ]; then
    ok I22 "用户提示单出口（${I22_OUT#\[OK\] I22: }）"
  else
    fail I22 "有 hook 绕过 user_notice 直接对用户说话，或安装面漏了它依赖的模块:
$I22_OUT"
  fi
else
  fail I22 "scripts/check_user_notice_exit.py 缺失 —— 用户提示单出口失去机检"
fi

# ── I23: 项目级 agent 模板是分发版的前缀扩展（.claude/agents 在本仓库里整份顶替同名的
#        plugin/agents；分发版改了而项目级没跟上，本仓库派出去的永远是旧模板——这个目录
#        曾因 22 份过期副本被整个删掉过一次）──
if [ -f scripts/check_project_agents.py ]; then
  I23_OUT="$(python3 scripts/check_project_agents.py 2>&1)"
  if [ $? -eq 0 ]; then
    ok I23 "项目级 agent 模板（${I23_OUT#\[OK\] I23: }）"
  else
    fail I23 "项目级 agent 模板缺失或与分发版漂移 —— 本仓库里派工会拿到过期模板:
$I23_OUT"
  fi
else
  fail I23 "scripts/check_project_agents.py 缺失 —— 项目级模板漂移将无人拦截"
fi

# ── I24: 注入清洗只许两份（事故: 信道那份清洗修成整类替换 Unicode C 类字符后，会话启动与
#        子 agent 注入各自那份仍只折叠空白，RLO/零宽字符/控制字节照样进模型上下文；各写各的
#        清洗函数就是这个洞的来路）。两份是分层边界（0925 Leader 裁定）：hook 侧
#        hook_core._sanitize_inline（三份逐字节副本，hook 只能用标准库）与服务端
#        services/notices/render.clean_text，二者输出由 tests/unit/test_sanitize_parity.py 对钉。
#        两道检查：
#        ① 按名字：这两个名字的定义（def 或 lambda 赋值）只许在各自的家里。边界：它只认名字，
#           换个名字另写一份抓不到，那一类靠 ②。已知例外：user_notice.py 三份副本里的
#           clean_text 是服务端 render.clean_text 的 hook 侧镜像（本地提示渲染用，早于本条存在），
#           同样由对钉测试管住，是否并入 hook_core 待定。
#        ② 按语义：非测试代码里 unicodedata.category 只许出现在下列白名单文件（含 import 别名
#           与 from unicodedata import category）。新写一份按类别过滤的清洗器，无论叫什么都红。
#           白名单里 user_notice.py 是用户行门槛（另一用途），channel_unread_codex.py 的 _quoted
#           是已知例外，待并入 hook_core。tests/ 不扫：测试用它做判据与对照实现。
#           边界：② 只认 category 的直接引用；str.isprintable 之类不经 unicodedata 的写法、
#           getattr 取 category 的写法都不在视野内，靠审查。
#        扫描含未跟踪文件，语法解析 ──
I24_OUT="$(python3 - <<'EOF'
import ast, subprocess, sys

HOMES = {
    "_sanitize_inline": {"plugin/hooks/hook_core.py", "src/aiteam/hooks/hook_core.py",
                         "plugin/harness/codex/hooks/hook_core.py"},
    "clean_text": {"src/aiteam/services/notices/render.py", "plugin/hooks/user_notice.py",
                   "src/aiteam/hooks/user_notice.py", "plugin/harness/codex/hooks/user_notice.py"},
}
CATEGORY_ALLOWED = HOMES["_sanitize_inline"] | HOMES["clean_text"] | {
    "plugin/harness/codex/hooks/channel_unread_codex.py",
}
files = subprocess.run(
    ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py"],
    capture_output=True, text=True, check=True,
).stdout.splitlines()
defined, problems, category_files = set(), [], set()
for path in files:
    try:
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
    except (OSError, SyntaxError, UnicodeError):
        continue
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names if a.name == "unicodedata"}
    for node in ast.walk(tree):
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda):
            name = next((t.id for t in node.targets if isinstance(t, ast.Name)), None)
        if name in HOMES:
            if path in HOMES[name]:
                defined.add((name, path))
            else:
                problems.append(f"{path}:{node.lineno}: 另写了一份 {name}，应改为引用已有的那一份")
        if path.startswith("tests/"):
            continue
        uses = (
            (isinstance(node, ast.Attribute) and node.attr == "category"
             and isinstance(node.value, ast.Name) and node.value.id in aliases)
            or (isinstance(node, ast.ImportFrom) and node.module == "unicodedata"
                and any(a.name == "category" for a in node.names))
        )
        if uses:
            category_files.add(path)
            if path not in CATEGORY_ALLOWED:
                problems.append(f"{path}:{node.lineno}: 用了 unicodedata.category，"
                                "像是又一份按类别过滤的清洗器；应引用 hook_core 或 clean_text")
for name, homes in HOMES.items():
    for path in sorted(homes - {p for n, p in defined if n == name}):
        problems.append(f"{path}: 缺少 {name} 定义")
if problems:
    print("\n".join(problems))
    sys.exit(1)
print(f"_sanitize_inline 3 份 + clean_text 1+3 份（服务端 + user_notice 镜像），别处 0 份；unicodedata.category 仅见于白名单 {len(category_files)} 个文件")
EOF
)"
if [ $? -eq 0 ]; then
  ok I24 "注入清洗只许两份（${I24_OUT}）"
else
  fail I24 "注入清洗出现第三份实现，或白名单外按类别过滤:
$I24_OUT"
fi

echo
if [ "$FAIL" -eq 1 ]; then
  echo "结论: ❌ 存在红线违规，禁止提交/发布。修复后重跑 bash scripts/check_invariants.sh"
  exit 1
fi
echo "结论: ✅ 全部不变量通过"
