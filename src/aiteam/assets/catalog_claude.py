"""The Claude Code config folder's catalog: which paths are the user's assets.

Measured on a real folder before it was written: next to the user's files, OS
installs agents, commands, skills, ``loop.md`` and its hook chain there, the
account syncs skills into ``skills/synced/``, and Claude Code keeps credentials,
caches and session state beside them. OS's own files are told apart by content
(``aiteam.assets.os_manifest``); the deny table below keeps everything else out.

``settings.json`` and the global config file are extraction sources: only their
``hooks`` and ``mcpServers`` keys are read, through a whitelist projection
(``aiteam.assets.extract``). The global config file sits inside a configured folder;
for the default folder it sits next to it (``aiteam.paths``).
"""

from __future__ import annotations

import os

from aiteam.assets.catalog import CategoryRule, DenyRule, HostCatalog
from aiteam.paths import CC_DEFAULT_GLOBAL_CONFIG, cc_config_dir, cc_main_chain_dir
from aiteam.types import AssetCategory, AssetHost

# The global config file's name, and the hook chain OS installs (relative to the folder).
GLOBAL_CONFIG_NAME = os.path.basename(CC_DEFAULT_GLOBAL_CONFIG)
MAIN_CHAIN = cc_main_chain_dir().relative_to(cc_config_dir()).as_posix()
SETTINGS = "settings.json"
PLUGIN_LISTS = ("plugins/installed_plugins.json", "plugins/known_marketplaces.json")

CATEGORIES: tuple[CategoryRule, ...] = (
    CategoryRule(AssetCategory.GLOBAL_INSTRUCTIONS, ("CLAUDE.md",)),
    CategoryRule(AssetCategory.LOOP_PROMPT, ("loop.md",)),
    CategoryRule(AssetCategory.PROJECT_MEMORY, ("projects/*/memory/**/*",)),
    CategoryRule(AssetCategory.SKILLS, ("skills/*/**/*",), excludes=("skills/synced/**/*",)),
    CategoryRule(AssetCategory.AGENTS, ("agents/*.md",)),
    CategoryRule(AssetCategory.COMMANDS, ("commands/**/*",)),
    CategoryRule(AssetCategory.HOOK_SCRIPTS, ("hooks/**/*",), excludes=(f"{MAIN_CHAIN}/**/*",)),
    CategoryRule(AssetCategory.HOOK_REGISTRATION, (SETTINGS,), extractor="settings_hooks"),
    CategoryRule(AssetCategory.MCP_SERVERS, (GLOBAL_CONFIG_NAME,), extractor="mcp_servers"),
    CategoryRule(AssetCategory.PLUGIN_MANIFEST, PLUGIN_LISTS, extractor="plugin_manifest"),
)

DENY: tuple[DenyRule, ...] = (
    DenyRule("sessions/**/*", ("sessions/4242.json",), "session registry and its keys"),
    DenyRule(".credentials.json", (".credentials.json",), "login credentials"),
    DenyRule("usage-cache/**/*", ("usage-cache/2026-11.json",), "status line ledger, the only copy"),
    DenyRule("usage-merge.json", ("usage-merge.json",), "status line ledger, the only copy"),
    DenyRule("plugins/**/*", ("plugins/marketplaces/m/plugin.json", "plugins/data/p/state.json"),
             "installed plugin files (reinstalled from the plugin list)", unless=PLUGIN_LISTS),
    DenyRule("paste-cache/**/*", ("paste-cache/a.txt",), "pasted content"),
    DenyRule("session-env/**/*", ("session-env/s/env.sh",), "session environment"),
    DenyRule("ide/**/*", ("ide/1234.lock",), "IDE connection state"),
    DenyRule("state/**/*", ("state/x.json",), "Claude Code state"),
    DenyRule("dev-mods/**/*", ("dev-mods/m/index.js",), "development mods"),
    DenyRule("mcp-needs-auth-cache.json", ("mcp-needs-auth-cache.json",), "cache"),
    DenyRule("model-prices-cache.json", ("model-prices-cache.json",), "cache"),
    DenyRule("skills/synced/**/*", ("skills/synced/s/SKILL.md",), "synced by the account, back on login"),
    DenyRule(f"{MAIN_CHAIN}/**/*", (f"{MAIN_CHAIN}/send_event.py",), "OS's hook chain, reinstalled"),
)

CATALOG = HostCatalog(host=AssetHost.CLAUDE, version=1, categories=CATEGORIES, deny=DENY)
