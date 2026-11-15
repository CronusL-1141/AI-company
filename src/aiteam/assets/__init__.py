"""Workflow asset backup: what the user wrote into a host's config folder, versioned.

A config folder (Claude Code's ``CLAUDE_CONFIG_DIR``, a Codex home) holds the user's
instructions, project memory, skills, agents, hooks and MCP definitions next to
caches, credentials and whatever OS installed there itself. Once the user enrolls a
folder, its assets are copied by content into the OS data directory and every change
becomes a version; nothing else in the folder is read.

* ``catalog`` (with ``catalog_claude`` and ``catalog_codex``): which relative paths
  are assets of which category; a deny table comes first and wins;
* ``os_manifest``: the files OS installs itself, derived from ``plugin/``;
* ``secret_scan``: rules that hold back content that looks like a credential;
* ``extract``: the parts of config files that are assets (hook registrations, MCP
  definitions, plugin lists), as canonical JSON;
* ``blob_store``: the content store, by SHA-256;
* ``attribution``: which OS project a project-memory folder belongs to;
* ``walk``: the comparison's stat-only walk of the managed paths;
* ``process``: reading, hashing, scanning and storing one batch, in a thread or a
  child process;
* ``ingest``: one job: what changed, in batches, recorded per batch;
* ``service``: when that happens (hook events, session boundaries, explicit calls).

Triggers come only from events and explicit calls: nothing here starts a timer, a
thread or a periodic task (machine-checked).

Importing this package imports nothing: the child process loads ``process`` alone.
"""
