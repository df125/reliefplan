"""Persistent staff roster: accumulates all staff seen over time."""
from __future__ import annotations

import json

from .storage import atomic_write_json, data_dir

ROSTER_PATH = data_dir() / "roster.json"


def load_roster() -> dict:
    if not ROSTER_PATH.exists():
        return {}
    try:
        return json.loads(ROSTER_PATH.read_text())
    except Exception:
        return {}


def save_roster(data: dict) -> None:
    atomic_write_json(ROSTER_PATH, data)


def merge_roster(existing: dict, staff: list) -> dict:
    """Merge a list of staff dicts into the existing roster, incrementing seen_count."""
    result = {k: dict(v) for k, v in existing.items()}
    for s in staff:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        entry = result.get(name, {"role": "", "shift_type": "", "seen_count": 0})
        entry["role"] = s.get("role") or entry["role"]
        shift = s.get("shiftType") or s.get("shift_type") or ""
        if shift:
            entry["shift_type"] = shift
        entry["seen_count"] = entry.get("seen_count", 0) + 1
        result[name] = entry
    return result
