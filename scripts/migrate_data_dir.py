#!/usr/bin/env python3
"""Manual entry for the OS data directory migration (dry run by default).

    python3 scripts/migrate_data_dir.py              # judge gates 1-5, change nothing
    python3 scripts/migrate_data_dir.py --apply      # migrate; gate 7 failing rolls back by itself
    python3 scripts/migrate_data_dir.py --rollback   # move a migrated directory back
    python3 scripts/migrate_data_dir.py --from PATH  # old directory outside the default legacy location

Same implementation as ``python3 -m aiteam.data_dir_migration``; see
``src/aiteam/data_dir_migration.py`` for the seven gates and their reason codes.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Run the code of the checkout this script belongs to, not whichever copy is installed.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aiteam.data_dir_migration import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
