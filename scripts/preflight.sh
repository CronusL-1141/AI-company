#!/usr/bin/env bash
# 本地预检 — push 前一次性跑完 CI 会跑的所有门禁，把"提交后才发现红"提前到本地。
#
# 用法：
#   bash scripts/preflight.sh          # 全量（ruff + eslint + 单测 + 机检）
#   bash scripts/preflight.sh --fast   # 跳过单测，只跑 lint + 机检（秒级）
#
# 装成 git pre-push 钩子（推荐）：
#   ln -sf ../../scripts/preflight.sh .git/hooks/pre-push
# 想临时跳过：git push --no-verify
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 1

FAST=0
[[ "${1:-}" == "--fast" ]] && FAST=1

FAIL=0
run() {
  local name="$1"; shift
  printf '\033[1m▶ %s\033[0m\n' "$name"
  if "$@"; then
    printf '\033[32m  ✓ %s\033[0m\n\n' "$name"
  else
    printf '\033[31m  ✗ %s 失败\033[0m\n\n' "$name"
    FAIL=1
  fi
}

# 1. ruff（对齐 CI：ruff check src/ tests/）——本机未装则跳过（CI 兜底），
# 工具缺失≠代码有问题，不该拦 push（2026-07-10 实测误拦）
if command -v ruff >/dev/null 2>&1; then
  run "ruff (src/ tests/)" ruff check src/ tests/
elif python3 -m ruff --version >/dev/null 2>&1; then
  run "ruff (src/ tests/)" python3 -m ruff check src/ tests/
else
  printf '\033[33m⏭  ruff 未安装，跳过（CI 会跑）\033[0m\n\n'
fi

# 2. eslint（对齐 CI：dashboard npm run lint）
run "eslint (dashboard)" bash -c 'cd dashboard && npm run lint'

# 3. 红线不变量机检（hook 双副本/版本锁步/双 dist/dist 时效/venv）
run "红线机检" bash scripts/check_invariants.sh

# 4. 单测（对齐 CI：pytest tests/unit/）— --fast 时跳过
if [[ "$FAST" == "0" ]]; then
  run "单测 (tests/unit/)" python3 -m pytest tests/unit/ -q
else
  printf '\033[33m⏭  单测已跳过（--fast）\033[0m\n\n'
fi

if [[ "$FAIL" == "1" ]]; then
  printf '\033[31m\033[1m✗ 预检未通过 — 修复后再 push（或 git push --no-verify 强推）\033[0m\n'
  exit 1
fi
printf '\033[32m\033[1m✓ 预检全部通过 — 可以 push\033[0m\n'
