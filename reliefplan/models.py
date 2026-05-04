from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List


@dataclass
class OperatingRoom:
    id: int
    building: str          # "Legacy" | "Lunder"
    floor: str             # "THOR" | "Gray" | "Jackson" | "L2" | "L3" | "L4"
    daytime_attending: str = ""
    daytime_crna: Optional[str] = None
    daytime_resident: Optional[str] = None
    is_late_running: bool = True
    flagged_complex: bool = False
    flagged_fluoro: bool = False
    estimated_end: Optional[str] = None
    is_new_start: bool = False   # case has not yet begun; attending capped at 1 new-start room


@dataclass
class StaffMember:
    name: str
    role: str              # "attending" | "CRNA" | "resident"
    resident_level: Optional[str] = None   # "R2" | "R3" | "R4"
    shift_type: str = ""   # "1-A" | "2-A" | "7a-7p" | "3p-10p" | "CRNA-7a-8p" | "CRNA-5p-8p"
    available_past_5pm: bool = True
    restrictions: List[str] = field(default_factory=list)   # e.g. ["no-fluoro"]
    affinities: List[str] = field(default_factory=list)     # e.g. ["L3", "thoracic"]
    daytime_or: Optional[int] = None
    already_deployed: bool = False   # 2-A / 3p-10p placed in a room before 5pm


@dataclass
class Assignment:
    or_id: int
    attending: str
    attending_role: str              # "supervisor" | "solo"
    physical_provider: Optional[str] = None
    physical_provider_type: Optional[str] = None   # "CRNA" | "resident" | None
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
    or_id: Optional[int] = None


@dataclass
class CoveragePlan:
    assignments: List[Assignment]
    unassigned_rooms: List[int]        # or_ids with no attending coverage
    no_physical_rooms: List[int]       # or_ids with no physical provider
    warnings: List[CoverageWarning]
    relief_entries: List[ReliefEntry]
    supervisor_groups: dict            # attending_name -> List[int] (or_ids supervised)
