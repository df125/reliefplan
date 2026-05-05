"""Persistent affinity database: staff floor/building preferences."""
from __future__ import annotations

import json
from pathlib import Path

AFFINITIES_PATH = Path(__file__).parent.parent / "affinities.json"


def load_affinities() -> dict[str, list[str]]:
    if not AFFINITIES_PATH.exists():
        return {}
    try:
        data = json.loads(AFFINITIES_PATH.read_text())
        return {k: list(v) for k, v in data.items() if isinstance(v, list)}
    except (json.JSONDecodeError, ValueError):
        return {}


def save_affinities(data: dict[str, list[str]]) -> None:
    AFFINITIES_PATH.write_text(json.dumps(data, indent=2))


def merge_affinities(
    existing: dict[str, list[str]],
    updates: list[dict],
) -> dict[str, list[str]]:
    """Apply add/remove affinity updates. Returns a new dict."""
    result = {k: list(v) for k, v in existing.items()}
    for u in updates:
        name = u.get("name", "")
        if not name:
            continue
        current = result.get(name, [])
        for tag in u.get("add", []):
            if tag not in current:
                current.append(tag)
        for tag in u.get("remove", []):
            if tag in current:
                current.remove(tag)
        result[name] = current
    return result
