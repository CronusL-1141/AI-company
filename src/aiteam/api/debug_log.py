"""AI Team OS — Debug logging to file.

Configures a file handler for all aiteam loggers.
Log file: debug.log in the OS data directory (rotated at 5MB, keep 3).
"""

import copy
import logging
import os
import re
from logging.handlers import RotatingFileHandler

from aiteam.paths import os_data_dir


class _DebugLogFormatter(logging.Formatter):
    # Uvicorn formats the client, request line and status into this message. Match
    # the whole message so an unexpected format cannot leave a query unfiltered.
    _access_line = re.compile(r'(?P<start>.*? - "[^"\s]+ )(?P<target>\S+)(?P<end> HTTP/[^"\s]+" \d+)')

    def format(self, record):
        # A record is shared by all handlers. Copy before formatting, including on
        # Python 3.11 where a Filter cannot return a replacement LogRecord.
        record = copy.copy(record)
        if record.name == "uvicorn.access":
            try:
                match = self._access_line.fullmatch(record.getMessage())
            except Exception:
                match = None
            if match is None:
                record.msg = "[unrecognized uvicorn access record]"
            else:
                # Search text and unknown parameters can be private too; remove
                # the entire query instead of maintaining a credential blacklist.
                record.msg = match["start"] + match["target"].partition("?")[0] + match["end"]
            record.args = ()
        return super().format(record)


def setup_debug_log(level=logging.DEBUG):
    """Configure file-based debug logging for the entire aiteam package.

    Raises DataDirMissing when there is no OS data directory: the API does not
    start on a new empty one (docs/config-dir-compat-design.md §2).
    """
    log_file = os_data_dir() / "debug.log"

    # create_app() may run more than once per process. A second handler on the same
    # file would duplicate every line and rotate it once per handler.
    aiteam_logger = logging.getLogger("aiteam")
    handler = next((
        existing for existing in aiteam_logger.handlers
        if isinstance(existing, RotatingFileHandler)
        and existing.baseFilename == os.path.abspath(log_file)
    ), None)
    if handler is None:
        handler = RotatingFileHandler(
            str(log_file), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        formatter = _DebugLogFormatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
    handler.setLevel(level)

    # Attach to root aiteam logger
    aiteam_logger.addHandler(handler)
    aiteam_logger.setLevel(level)

    # Also capture uvicorn access/error
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        logging.getLogger(name).addHandler(handler)

    return log_file
