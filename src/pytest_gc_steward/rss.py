"""Current resident set size of this process, in megabytes."""

from __future__ import annotations

import os
import sys

_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _linux() -> float | None:
    try:
        with open("/proc/self/statm", "rb") as f:
            return int(f.read().split()[1]) * _PAGE / 1048576
    except (OSError, ValueError, IndexError):
        return None


def _psutil() -> float | None:
    try:
        import psutil
    except ImportError:
        return None
    return psutil.Process().memory_info().rss / 1048576


def _peak() -> float | None:
    """Peak RSS: a last resort; it never goes down."""
    try:
        import resource
    except ImportError:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return peak / (1048576 if sys.platform == "darwin" else 1024)


def current_rss_mb() -> float | None:
    """Best available measure of the current RSS, or ``None`` if unknown."""
    if sys.platform.startswith("linux"):
        value = _linux()
        if value is not None:
            return value
    value = _psutil()
    if value is not None:
        return value
    return _peak()
