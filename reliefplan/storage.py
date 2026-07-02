"""Shared helpers for the small JSON data files (roster, affinities)."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    """Directory for runtime data files.

    Defaults to the repository root (alongside the package) for backwards
    compatibility; set RELIEFPLAN_DATA_DIR for read-only or containerized
    installs.
    """
    override = os.environ.get("RELIEFPLAN_DATA_DIR")
    return Path(override) if override else Path(__file__).parent.parent


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON via temp-file + rename so concurrent readers never see a
    partial file and a crash mid-write can't corrupt the existing one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
