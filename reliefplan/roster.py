"""Persistent staff roster: accumulates all staff seen over time."""
from __future__ import annotations

import json
from pathlib import Path

ROSTER_PATH = Path(__file__).parent.parent / "roster.json"


def load_roster() -> dict:
    if not ROSTER_PATH.exists():
        return {}
    try:
        return json.loads(ROSTER_PATH.read_text())
    except Exception:
        return {}


def save_roster(data: dict) -> None:
    ROSTER_PATH.write_text(json.dumps(data, indent=2, sort_keys=True))


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
