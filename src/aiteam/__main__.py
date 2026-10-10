"""``python3 -m aiteam``: the ``aiteam`` command, for installs whose scripts directory
is not on PATH. Run it with the interpreter that runs the OS (its MCP server command)."""

from aiteam.cli.app import app

app(prog_name="aiteam")
