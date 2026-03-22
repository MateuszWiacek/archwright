"""Shared constants used across the backup package."""

from __future__ import annotations

import stat
import zipfile
from datetime import datetime

EXIT_SUCCESS = 0
EXIT_ERROR = 1

TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"

ZIP_COMPRESSION = zipfile.ZIP_DEFLATED
ZIP_COMPRESS_LEVEL = 9
STREAM_CHUNK_SIZE = 1024 * 1024  # 1 MiB chunks for streaming into ZIP

# Stored in ZipInfo.external_attr as a regular file with 0644 permissions.
ZIP_EXTERNAL_ATTR = (stat.S_IFREG | 0o644) << 16

ZIP_MIN_TIMESTAMP = datetime(1980, 1, 1, 0, 0, 0)
ZIP_MAX_TIMESTAMP = datetime(2107, 12, 31, 23, 59, 59)

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_LEVEL = "INFO"

DEFAULT_HOOK_TIMEOUT = 300
DEFAULT_DUMP_TIMEOUT = 3600
