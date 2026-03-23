#!/usr/bin/env bash
# Auto-install Python dependencies into ${CLAUDE_PLUGIN_DATA}/venv
# Cross-platform: Windows (Scripts/) + macOS/Linux (bin/)
set -e

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT}"
PLUGIN_DATA="${CLAUDE_PLUGIN_DATA}"

mkdir -p "${PLUGIN_DATA}"

# Check if requirements changed (skip install if identical)
if diff -q "${PLUGIN_ROOT}/requirements.txt" "${PLUGIN_DATA}/requirements.txt" >/dev/null 2>&1; then
  exit 0
fi

echo "[AI Team OS] Installing dependencies..." >&2

cd "${PLUGIN_DATA}"

# Create venv if not exists
if [ ! -d "venv" ]; then
  python -m venv venv 2>/dev/null || python3 -m venv venv
fi

# Cross-platform pip path
if [ -f "venv/Scripts/pip.exe" ]; then
  PIP="venv/Scripts/pip"
else
  PIP="venv/bin/pip"
fi

# Install requirements
$PIP install -q -r "${PLUGIN_ROOT}/requirements.txt"

# Install aiteam package — try local source first, then GitHub
if [ -f "${PLUGIN_ROOT}/../pyproject.toml" ]; then
  # Local dev: project root is plugin parent
  $PIP install -q -e "${PLUGIN_ROOT}/.."
elif [ -f "${PLUGIN_ROOT}/../src/aiteam/__init__.py" ]; then
  # Local dev: src dir exists
  $PIP install -q -e "${PLUGIN_ROOT}/.."
else
  # Plugin installed via marketplace: install from GitHub
  $PIP install -q "git+https://github.com/CronusL-1141/AI-company.git"
fi

# Save installed version marker
cp "${PLUGIN_ROOT}/requirements.txt" "${PLUGIN_DATA}/requirements.txt"

echo "[AI Team OS] Dependencies installed successfully." >&2
