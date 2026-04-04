"""AI Team OS — Debug logging to file.

Configures a file handler for all aiteam loggers.
Log file: ~/.claude/data/ai-team-os/debug.log (rotated at 5MB, keep 3)
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_debug_log(level=logging.DEBUG):
    """Configure file-based debug logging for the entire aiteam package."""
    log_dir = Path.home() / ".claude" / "data" / "ai-team-os"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "debug.log"

    handler = RotatingFileHandler(
        str(log_file), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setLevel(level)
    formatter = logging.Formatter(
        "%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    # Attach to root aiteam logger
    aiteam_logger = logging.getLogger("aiteam")
    aiteam_logger.addHandler(handler)
    aiteam_logger.setLevel(level)

    # Also capture uvicorn access/error
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addHandler(handler)

    return log_file
