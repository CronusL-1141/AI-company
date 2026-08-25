# Security Policy

## Supported Versions

Only the latest release line receives security fixes.

| Version | Supported |
| ------- | --------- |
| 1.11.x  | yes       |
| < 1.11  | no        |

## What this project touches on your machine

AI Team OS is local-first. Knowing its footprint helps you judge what counts as a vulnerability:

- Everything runs and stays on your machine: the SQLite database, reports, memory and logs live under `~/.claude/data/ai-team-os/`. There is no telemetry and no external service. Network calls happen only for features you invoke explicitly (e.g. GitHub scanning in the ecosystem module).
- The installer registers Claude Code hooks (plain Python scripts under `~/.claude/hooks/ai-team-os/`, readable before you run anything) and an MCP server. None of them download or execute remote code.
- The API server binds to localhost only.

Reports about hook behavior, MCP tool injection surfaces, or anything that widens this footprint are very welcome.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting (Security tab -> "Report a vulnerability") instead of opening a public issue.

Include: affected version, install method (plugin / source), OS, reproduction steps, and impact as you understand it.

Response is best-effort within 7 days. Fixes ship as patch releases; reporters are credited in the changelog unless they prefer otherwise.
