from __future__ import annotations
from typing import Set

FLOOR_ORS: dict[str, list[int]] = {
    "THOR":    [4, 14, 15, 16, 43, 44],
    "Gray":    [5, 6, 7, 10, 11],
    "Jackson": [12] + list(range(32, 43)) + [48],  # 12, 32-42, 48 (gen surg in Cardiac section)
    "L2":      list(range(51, 55)),                 # 51-54
    "L3":      list(range(61, 74)),                 # 61-73
    "L4":      [17] + list(range(81, 92)),          # 17, 81-91
    "IR":      list(range(201, 210)),               # virtual IDs for evening IR rooms
    "Endo":    list(range(211, 220)),               # virtual IDs for evening Endo rooms
}

OR_TO_FLOOR: dict[int, str] = {}
OR_TO_BUILDING: dict[int, str] = {}

_OFFSITE_FLOORS = frozenset({"IR", "Endo"})

for _floor, _ors in FLOOR_ORS.items():
    if _floor in _OFFSITE_FLOORS:
        _bldg = _floor
    else:
        _bldg = "Legacy" if _floor in ("THOR", "Gray", "Jackson") else "Lunder"
    for _or_id in _ors:
        OR_TO_FLOOR[_or_id] = _floor
        OR_TO_BUILDING[_or_id] = _bldg

LEGACY_FLOORS: frozenset[str] = frozenset({"THOR", "Gray", "Jackson"})
LUNDER_FLOORS: frozenset[str] = frozenset({"L2", "L3", "L4"})


def get_floor(or_id: int) -> str:
    return OR_TO_FLOOR.get(or_id, "Unknown")


def get_building(or_id: int) -> str:
    return OR_TO_BUILDING.get(or_id, "Unknown")


def floors_compatible(floors: Set[str]) -> bool:
    """Return False if the set of Lunder floors violates the L2+L4-without-L3 rule."""
    lunder = floors & LUNDER_FLOORS
    if "L2" in lunder and "L4" in lunder and "L3" not in lunder:
        return False
    return True


def same_building(floor_a: str, floor_b: str) -> bool:
    if floor_a in LEGACY_FLOORS and floor_b in LEGACY_FLOORS:
        return True
    if floor_a in LUNDER_FLOORS and floor_b in LUNDER_FLOORS:
        return True
    if floor_a == floor_b and floor_a in _OFFSITE_FLOORS:
        return True
    return False
