"""Resolves which code is actually running, and when this process started.

kolmogorov has no .git checkout (deploys are rsync'd, not cloned), so a VERSION file written at
deploy time takes priority; local dev falls back to a live git rev-parse. Capturing process start
time here, once, means "did this incident start shortly after a restart" can be answered by
reading a span attribute rather than needing a separate deploy-event table.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _detect_version() -> str:
    version_file = _ROOT / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    try:
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], cwd=_ROOT, stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


SERVICE_VERSION = _detect_version()
SERVICE_STARTED_AT = time.time()
