from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OperatingRoom:
    id: int
    building: str          # "Legacy" | "Lunder"
    floor: str             # "THOR" | "Gray" | "Jackson" | "L2" | "L3" | "L4"
    daytime_attending: str = ""
    daytime_crna: str | None = None
    daytime_resident: str | None = None
    is_late_running: bool = True
    flagged_complex: bool = False
    flagged_fluoro: bool = False
    estimated_end: str | None = None
    is_new_start: bool = False   # case has not yet begun; attending capped at 1 new-start room


@dataclass
class StaffMember:
    name: str
    role: str              # "attending" | "CRNA" | "resident"
    resident_level: str | None = None   # "R2" | "R3" | "R4"
    shift_type: str = ""   # "1-A" | "2-A" | "7a-7p" | "3p-10p" | "CRNA-7a-8p" | "CRNA-5p-8p"
    available_past_5pm: bool = True
    restrictions: list[str] = field(default_factory=list)   # e.g. ["no-fluoro"]
    affinities: list[str] = field(default_factory=list)     # e.g. ["L3", "thoracic"]
    daytime_or: int | None = None
    already_deployed: bool = False   # 2-A / 3p-10p placed in a room before 5pm
    is_moonlighter: bool = False
    departure_target: str = ""       # e.g. "22:00", display-only
    daytime_location: str = ""       # "EP" | "IR" | "Endo" | "" for main OR


@dataclass
class Assignment:
    or_id: int
    attending: str
    attending_role: str              # "supervisor" | "solo"
    physical_provider: str | None = None
    physical_provider_type: str | None = None   # "CRNA" | "resident" | None
    continuity: bool = False         # provider was in this room during the day


@dataclass
class ReliefEntry:
    or_id: int
    relieved_name: str
    relieved_role: str   # "attending" | "CRNA" | "resident"


@dataclass
class CoverageWarning:
    severity: str   # "error" | "warning" | "info"
    message: str
    or_id: int | None = None


@dataclass
class CoveragePlan:
    assignments: list[Assignment]
    unassigned_rooms: list[int]        # or_ids with no attending coverage
    no_physical_rooms: list[int]       # or_ids with no physical provider
    warnings: list[CoverageWarning]
    relief_entries: list[ReliefEntry]
    supervisor_groups: dict            # attending_name -> List[int] (or_ids supervised)
