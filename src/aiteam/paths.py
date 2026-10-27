"""Where the OS data, the Claude Code config folder and the Codex home are.

A re-export of the path section of ``aiteam.hooks.hook_core``: one definition,
kept byte-identical in the three hook_core copies (I1) and spelled nowhere else
(I30). Do not add logic here; a second resolver is how two processes end up
reading two databases (docs/config-dir-compat-design.md §2, §5.1).

Run time only resolves. Creating the data directory is reserved to these
entries (§5.2); the first three go through ``create_data_dir_if_absent``, which
resolves first and creates only when nothing exists anywhere
(tests/unit/test_paths_resolver.py pins the list of callers):

* ``install.py`` (step 5) and ``scripts/codex_adapter.py install``;
* ``plugin/hooks/auto_install.py`` on its first run;
* an explicit ``AITEAM_DATA_DIR`` or ``AITEAM_DB_PATH`` (tests and tools).

The API serves sessions of every Claude Code config folder: per request it binds
the folder of the session the request is about (``bind_cc_config_dir``, used only
by ``aiteam.api.cc_config_scope``), and ``cc_config_dir`` answers with it;
``own_cc_config_dir`` is the process's own, whatever is bound.

``python3 -m aiteam.paths`` is the same report as ``aiteam paths``.
"""

from __future__ import annotations

from aiteam.hooks.hook_core import (
    CC_DEFAULT_CONFIG_DIR,
    CC_DEFAULT_GLOBAL_CONFIG,
    CHAIN_OWNER_FILE,
    CHAIN_OWNERS,
    CODEX_DEFAULT_HOME,
    DATA_DIR_ENV,
    DATA_DIR_MODES,
    DB_NAME,
    DEFAULT_DATA_DIR,
    INSTALLS_FILE,
    LEGACY_DATA_DIR,
    MIGRATION_LOCK,
    MOVED_MARKER,
    DataDirCandidate,
    DataDirMissing,
    DataDirResolution,
    api_port_file,
    bind_cc_config_dir,
    cc_config_dir,
    cc_global_config,
    cc_main_chain_dir,
    chain_owner,
    codex_home,
    create_data_dir_if_absent,
    inspect_data_dir,
    install_records,
    last_migration_attempt,
    main_chain_owner,
    migration_lock,
    os_data_dir,
    own_cc_config_dir,
    resolve_data_dir,
    unbind_cc_config_dir,
)

__all__ = [
    "CC_DEFAULT_CONFIG_DIR",
    "CC_DEFAULT_GLOBAL_CONFIG",
    "CHAIN_OWNERS",
    "CHAIN_OWNER_FILE",
    "CODEX_DEFAULT_HOME",
    "DATA_DIR_ENV",
    "DATA_DIR_MODES",
    "DB_NAME",
    "DEFAULT_DATA_DIR",
    "INSTALLS_FILE",
    "LEGACY_DATA_DIR",
    "MIGRATION_LOCK",
    "MOVED_MARKER",
    "DataDirCandidate",
    "DataDirMissing",
    "DataDirResolution",
    "api_port_file",
    "bind_cc_config_dir",
    "cc_config_dir",
    "cc_global_config",
    "cc_main_chain_dir",
    "chain_owner",
    "codex_home",
    "create_data_dir_if_absent",
    "inspect_data_dir",
    "install_records",
    "last_migration_attempt",
    "main_chain_owner",
    "migration_lock",
    "os_data_dir",
    "own_cc_config_dir",
    "resolve_data_dir",
    "unbind_cc_config_dir",
]

if __name__ == "__main__":
    import sys

    from aiteam.cli.commands.paths_cmd import main

    sys.exit(main(sys.argv[1:]))
