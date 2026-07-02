"""JSON input parsing and validation."""
from __future__ import annotations

import json
from pathlib import Path

from .models import OperatingRoom, StaffMember

VALID_BUILDINGS = {"Legacy", "Lunder", "IR", "Endo"}
VALID_FLOORS = {"THOR", "Gray", "Jackson", "L2", "L3", "L4", "IR", "Endo"}
VALID_ROLES = {"attending", "CRNA", "resident"}
VALID_SHIFT_TYPES = {"1-A", "2-A", "7a-7p", "3p-10p", "CRNA-7a-8p", "CRNA-5p-8p", "moonlighter"}
VALID_RESIDENT_LEVELS = {"R2", "R3", "R4"}

BUILDING_FLOOR_MAP = {
    "Legacy": {"THOR", "Gray", "Jackson"},
    "Lunder": {"L2", "L3", "L4"},
    "IR":     {"IR"},
    "Endo":   {"Endo"},
}


def load(path: Path) -> tuple[list[OperatingRoom], list[StaffMember]]:
    """Parse and validate a JSON input file. Raises ValueError on bad input."""
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

    rooms = [_parse_room(r, i) for i, r in enumerate(data.get("rooms", []))]
    staff = [_parse_staff(s, i) for i, s in enumerate(data.get("staff", []))]
    return rooms, staff


def _parse_room(d: dict, idx: int) -> OperatingRoom:
    label = f"rooms[{idx}]"

    def req(key: str):
        if key not in d:
            raise ValueError(f"{label}: missing required field '{key}'")
        return d[key]

    or_id = int(req("id"))
    building = req("building")
    floor = req("floor")

    if building not in VALID_BUILDINGS:
        raise ValueError(f"{label} OR {or_id}: building must be one of {VALID_BUILDINGS}")
    if floor not in VALID_FLOORS:
        raise ValueError(f"{label} OR {or_id}: floor must be one of {VALID_FLOORS}")
    if floor not in BUILDING_FLOOR_MAP[building]:
        raise ValueError(
            f"{label} OR {or_id}: floor '{floor}' is not in building '{building}'"
        )

    return OperatingRoom(
        id=or_id,
        building=building,
        floor=floor,
        daytime_attending=d.get("daytimeAttending", ""),
        daytime_crna=d.get("daytimeCRNA"),
        daytime_resident=d.get("daytimeResident"),
        is_late_running=d.get("isLateRunning", True),
        flagged_complex=d.get("flaggedComplex", False),
        flagged_fluoro=d.get("flaggedFluoro", False),
        estimated_end=d.get("estimatedEnd"),
        is_new_start=d.get("isNewStart", False),
    )


def _parse_staff(d: dict, idx: int) -> StaffMember:
    label = f"staff[{idx}]"

    def req(key: str):
        if key not in d:
            raise ValueError(f"{label}: missing required field '{key}'")
        return d[key]

    name = req("name")
    role = req("role")
    if role not in VALID_ROLES:
        raise ValueError(f"{label} '{name}': role must be one of {VALID_ROLES}")

    shift_type = d.get("shiftType", "")
    if shift_type and shift_type not in VALID_SHIFT_TYPES:
        raise ValueError(
            f"{label} '{name}': shiftType must be one of {VALID_SHIFT_TYPES} (or omitted)"
        )

    res_level = d.get("residentLevel")
    if res_level and res_level not in VALID_RESIDENT_LEVELS:
        raise ValueError(
            f"{label} '{name}': residentLevel must be one of {VALID_RESIDENT_LEVELS}"
        )

    daytime_location = d.get("daytimeLocation", "")
    if daytime_location not in ("EP", "IR", "Endo", ""):
        daytime_location = ""

    return StaffMember(
        name=name,
        role=role,
        resident_level=res_level,
        shift_type=shift_type,
        available_past_5pm=d.get("availablePast5pm", True),
        restrictions=d.get("restrictions", []),
        affinities=d.get("affinities", []),
        daytime_or=d.get("daytimeOR"),
        already_deployed=d.get("alreadyDeployed", False),
        is_moonlighter=bool(d.get("isMoonlighter", False)),
        departure_target=d.get("departureTarget", ""),
        daytime_location=daytime_location,
    )
