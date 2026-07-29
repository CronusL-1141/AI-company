#!/usr/bin/env bash
# README 数字机检 — 双语 README 的关键数字与代码实测对齐，漂移即红。
# 背景: 2026-07 审计发现 18 页/631+ 测试/30+ 生态工具三处数字腐烂，全部源于手工维护。
#
# 宽松设计防误报——只锚定四类高价值声明，普通措辞变化不误伤：
#   ① 版本: announcement 行（"> ⚡" 开头）必须包含 v{__version__}（硬等式）
#   ② MCP 工具数: "N MCP tools" / "N 个 MCP 工具" 声明 = @mcp.tool 实测数（硬等式）；
#      同一行数字前文含 ecosystem/生态 时按 ecosystem.py 单独实测数校验
#   ③ 测试数: "N+ tests" / "N+ 测试" 声明 ≤ pytest --collect-only 实收数（单向，防夸大；
#      pytest 不可用时跳过该项，CI 兜底）
#   ④ 页面数: "N pages" / "N 个页面" 声明 = App.tsx 路由实测数（硬等式）
#   ⑤ REST 端点数: "N REST endpoints" / "N 个 REST 端点" 与目录树的 "(N routes)"
#      = 应用自身 OpenAPI 路由表实测数（硬等式）。真相源必须是路由表而非 grep：
#      2026-07-27 实测静态 grep 只数出 195，路由表 199（多方法路由与换行装饰器漏网）。
#
# 用法: bash scripts/check_readme_numbers.sh   （仓库根目录执行；CI 与本地通用）
# 退出码: 0=全过, 1=有漂移

set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# ── 实测真相源 ──────────────────────────────────────────────
VERSION="$(python3 -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('src/aiteam/__init__.py').read()).group(1))")" || {
  echo "❌ 无法从 src/aiteam/__init__.py 读取 __version__"; exit 1; }
MCP_TOTAL="$(grep -h '@mcp.tool' src/aiteam/mcp/tools/*.py | wc -l | tr -d ' ')"
MCP_ECO="$(grep -c '@mcp.tool' src/aiteam/mcp/tools/ecosystem.py | tr -d ' ')"
PAGES="$(grep -c '<Route .*element={<ErrorBoundary>' dashboard/src/App.tsx | tr -d ' ')"
# 测试实收数（兼容 "N tests collected" 与 "collected N items" 两种输出）
TESTS="$(python3 -m pytest tests --collect-only -q 2>/dev/null | tail -5 \
  | grep -oE '[0-9]+ tests? collected|collected [0-9]+ items' \
  | grep -oE '[0-9]+' | head -1)" || TESTS=""
# REST 端点实测 = 应用自己的 OpenAPI 路由表里的 operation 数（path × method）。
# 走 app.openapi() 这一公开接口，不碰 FastAPI 的惰性 include 内部结构。
#
# 必须显式把**本仓的** src 顶到 sys.path 最前：aiteam 是 editable 安装，裸 import
# 解析到的是安装时登记的那个 checkout。在 git worktree 里跑（本仓的多会话并行纪律
# 要求第二个及之后的会话必须用 worktree）就会静默地去数主 checkout 的路由，于是
# 这一项永远"通过"——它核对的根本不是眼前这份代码。其余各项（工具数/页面数 grep、
# I12/I13）都已按 ROOT 取数，只有这里漏了。
REST="$(python3 -c '
import sys

sys.path.insert(0, "src")

from fastapi import FastAPI

from aiteam.api.routes import api_router

app = FastAPI()
app.include_router(api_router)
paths = app.openapi().get("paths", {})
verbs = ("get", "post", "put", "patch", "delete")
print(sum(1 for ops in paths.values() for m in ops if m.lower() in verbs))
' 2>/dev/null)" || REST=""

python3 - "$VERSION" "$MCP_TOTAL" "$MCP_ECO" "$PAGES" "${TESTS:-}" "${REST:-}" <<'PYEOF'
import re
import sys

version, mcp_total, mcp_eco, pages = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
tests = int(sys.argv[5]) if len(sys.argv) > 5 and sys.argv[5] else None
rest = int(sys.argv[6]) if len(sys.argv) > 6 and sys.argv[6] else None

RE_MCP = re.compile(r'(\d[\d,]*)\**\s*(?:个\s*)?MCP\s*(?:tools|工具)')
RE_TEST = re.compile(r'(\d[\d,]*)\+?\**\s*(?:automated\s+)?tests\b'
                     r'|(\d[\d,]*)\+?\**\s*(?:项|个|条)?\s*(?:自动化)?\s*测试')
RE_PAGE = re.compile(r'(\d[\d,]*)\**\s*(?:个\s*)?(?:[Dd]ashboard\s*)?(?:路由)?\s*(?:pages\b|页面)')
RE_ECO_CTX = re.compile(r'[Ee]cosystem|生态')
# ⑤ 两种写法都锚住：headline 的 "N REST endpoints"，与目录树的 "REST endpoints (N routes)"
RE_REST = re.compile(r'(\d[\d,]*)\**\s*(?:个\s*)?REST\s*(?:endpoints?\b|端点)')
RE_REST_ROUTES = re.compile(r'REST\s*(?:endpoints?|端点)\s*[（(](\d[\d,]*)\s*(?:routes?\b|条路由)')

fails = []
for path in ('README.md', 'README.zh-CN.md'):
    lines = open(path, encoding='utf-8').read().splitlines()
    # ① 版本声明
    ann = [l for l in lines if l.lstrip().startswith('> ⚡')]
    if not ann:
        fails.append(f'{path}: 找不到 announcement 行（"> ⚡" 开头）——无法核对版本声明')
    elif not any(f'v{version}' in l for l in ann):
        fails.append(f'{path}: announcement 行版本声明 ≠ 代码版本 v{version}'
                     f'（src/aiteam/__init__.py）——发版后请同步 README 顶部引用块')
    for i, line in enumerate(lines, 1):
        # ② MCP 工具数（行内前缀含 ecosystem/生态 → 按 ecosystem 单独数校验）
        for m in RE_MCP.finditer(line):
            n = int(m.group(1).replace(',', ''))
            is_eco = bool(RE_ECO_CTX.search(line[:m.start()]))
            expect = mcp_eco if is_eco else mcp_total
            label = f'ecosystem 工具实测数 {mcp_eco}' if is_eco else f'MCP 工具实测总数 {mcp_total}'
            if n != expect:
                fails.append(f'{path}:{i}: 声明 "{m.group(0).strip()}" ≠ {label}'
                             f'（grep @mcp.tool src/aiteam/mcp/tools/）')
        # ③ 测试数（单向: 声明 ≤ 实收，防吹大）
        if tests is not None:
            for m in RE_TEST.finditer(line):
                n = int((m.group(1) or m.group(2)).replace(',', ''))
                if n > tests:
                    fails.append(f'{path}:{i}: 测试数声明 "{m.group(0).strip()}" > 实收 {tests}'
                                 f'（pytest --collect-only）——README 数字超过了实际测试数')
        # ④ 页面数
        for m in RE_PAGE.finditer(line):
            n = int(m.group(1).replace(',', ''))
            if n != pages:
                fails.append(f'{path}:{i}: 页面数声明 "{m.group(0).strip()}" ≠ 路由实测数 {pages}'
                             f'（dashboard/src/App.tsx）')
        # ⑤ REST 端点数（两种写法同一真相源）
        if rest is not None:
            for regex in (RE_REST, RE_REST_ROUTES):
                for m in regex.finditer(line):
                    n = int(m.group(1).replace(',', ''))
                    if n != rest:
                        fails.append(f'{path}:{i}: REST 端点声明 "{m.group(0).strip()}" ≠ '
                                     f'路由表实测数 {rest}（app.openapi() 的 path × method）')

if fails:
    for f in fails:
        print(f'❌ {f}')
    print(f'\n结论: ❌ README 数字漂移 {len(fails)} 处——请按实测值更新双语 README')
    sys.exit(1)

skip = '' if tests is not None else '（⚠️ pytest 不可用，测试数校验已跳过，CI 兜底）'
skip_rest = '' if rest is not None else '（⚠️ 应用导入失败，REST 端点校验已跳过，CI 兜底）'
print(f'✅ README 数字机检通过（双语）: 版本 v{version} · MCP 工具 {mcp_total}（生态 {mcp_eco}）'
      f' · 页面 {pages} · REST 端点 {rest if rest is not None else "?"}{skip_rest}'
      f' · 测试声明 ≤ {tests if tests is not None else "?"}{skip}')
PYEOF
