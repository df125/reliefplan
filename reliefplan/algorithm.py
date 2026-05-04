"""
Core assignment algorithm for MGH Anesthesia 5PM Coverage Planner.

Steps mirror the specification exactly:
  1. Identify late ORs and relief needs
  2. Place CRNAs (continuity first, then pool)
  3. Place residents (after all CRNAs placed; R2 restricted to complex)
  4-6. Assign attendings (supervisors then solos)
  7. Optimization passes
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .models import (
    Assignment,
    CoveragePlan,
    CoverageWarning,
    OperatingRoom,
    ReliefEntry,
    StaffMember,
)
from .zones import LUNDER_FLOORS, floors_compatible, same_building


# ---------------------------------------------------------------------------
# Attending supervision state tracker
# ---------------------------------------------------------------------------

@dataclass
class _AttState:
    """Mutable supervision state for one attending during algorithm execution."""
    staff: StaffMember
    supervised_rooms: List[int] = field(default_factory=list)
    supervised_types: List[str] = field(default_factory=list)   # parallel: "CRNA"|"resident"
    solo_room: Optional[int] = None
    building: Optional[str] = None
    floors: Set[str] = field(default_factory=set)
    new_start_count: int = 0

    # --- derived properties ---

    @property
    def is_solo(self) -> bool:
        return self.solo_room is not None

    @property
    def resident_count(self) -> int:
        return self.supervised_types.count("resident")

    @property
    def total_supervised(self) -> int:
        return len(self.supervised_rooms)

    # --- feasibility checks ---

    def can_supervise(
        self,
        room: OperatingRoom,
        physical_type: str,   # "CRNA" | "resident"
        is_new_start: bool,
    ) -> bool:
        """Return True if this attending can take on supervision of `room`."""
        # Cannot supervise if already deployed as solo
        if self.is_solo:
            return False

        # Building: once locked, must match
        if self.building and self.building != room.building:
            return False

        # Lunder floor constraint: L2 + L4 without L3 forbidden
        if room.building == "Lunder":
            candidate_floors = self.floors | {room.floor}
            if not floors_compatible(candidate_floors):
                return False

        # Ratio constraints
        new_resident = self.resident_count + (1 if physical_type == "resident" else 0)
        new_total = self.total_supervised + 1
        if new_resident > 0 and new_total > 2:
            return False
        if new_resident == 0 and new_total > 4:
            return False

        # New-start cap: one attending cannot supervise > 1 new-start room
        if is_new_start and self.new_start_count >= 1:
            return False

        # Fluoro restriction: no-fluoro attending cannot cover a fluoro room
        if room.flagged_fluoro and "no-fluoro" in self.staff.restrictions:
            return False

        return True

    def can_go_solo(self, room: OperatingRoom) -> bool:
        """Return True if this attending can take the room solo (no other rooms)."""
        if self.is_solo or self.total_supervised > 0:
            return False
        if room.flagged_fluoro and "no-fluoro" in self.staff.restrictions:
            return False
        return True

    # --- mutators ---

    def add_supervised(self, room: OperatingRoom, physical_type: str, is_new_start: bool) -> None:
        self.supervised_rooms.append(room.id)
        self.supervised_types.append(physical_type)
        self.building = room.building
        self.floors.add(room.floor)
        if is_new_start:
            self.new_start_count += 1

    def set_solo(self, room: OperatingRoom) -> None:
        self.solo_room = room.id
        self.building = room.building
        self.floors.add(room.floor)


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

_SUPERVISION_PRIORITY = {"3p-10p": 0, "2-A": 1, "7a-7p": 2, "1-A": 3}
_SOLO_PRIORITY = {"7a-7p": 0, "2-A": 1, "3p-10p": 2, "1-A": 3}


def _supervision_score(att: _AttState, room: OperatingRoom, physical_type: str) -> int:
    """Higher score = better match of this attending supervising this room."""
    score = 0

    # Prefer attendings who are already locked to the same building / floor
    if att.building == room.building:
        score += 30
    if room.floor in att.floors:
        score += 50

    # Continuity: attending was in this room during the day
    if att.staff.daytime_or == room.id:
        score += 80

    # Affinities
    if room.floor in att.staff.affinities:
        score += 20
    if room.building in att.staff.affinities:
        score += 10

    # Prefer 3p-10p for heavy supervision, penalise 1-A for extra load
    shift = att.staff.shift_type
    if shift == "3p-10p":
        score += 15
    elif shift == "1-A":
        # Keep 1-A light; penalise if they already have rooms
        score -= att.total_supervised * 20

    # Prefer already-deployed attendings in their room (continuity rule 7)
    if att.staff.already_deployed and att.staff.daytime_or == room.id:
        score += 60

    return score


def _solo_score(att: _AttState, room: OperatingRoom) -> int:
    """Higher score = better match of this attending going solo in room."""
    score = 0
    if att.staff.daytime_or == room.id:
        score += 100   # strong continuity preference (spec rule 10)
    if att.staff.already_deployed and att.staff.daytime_or == room.id:
        score += 50
    if room.floor in att.staff.affinities:
        score += 20
    if room.building in att.staff.affinities:
        score += 10
    return score


# ---------------------------------------------------------------------------
# Physical-provider helpers
# ---------------------------------------------------------------------------

def _find_crna(
    room: OperatingRoom,
    pool: List[StaffMember],
    used: Set[str],
    staff_set: Set[str],
) -> Optional[StaffMember]:
    """Return best available CRNA for this room, or None."""
    candidates = [
        s for s in pool
        if s.name not in used
        and not (room.flagged_fluoro and "no-fluoro" in s.restrictions)
    ]
    if not candidates:
        return None

    def score(s: StaffMember) -> int:
        sc = 0
        # Continuity: daytime CRNA for this room
        if s.name == room.daytime_crna and s.name in staff_set:
            sc += 100
        if s.daytime_or == room.id:
            sc += 80
        # Affinity
        if room.floor in s.affinities:
            sc += 20
        if room.building in s.affinities:
            sc += 10
        # Prefer 7a-8p (continuity shift) for continuity rooms
        if s.shift_type == "CRNA-7a-8p" and s.daytime_or == room.id:
            sc += 40
        return sc

    return max(candidates, key=score)


def _find_resident(
    room: OperatingRoom,
    pool: List[StaffMember],
    used: Set[str],
) -> Optional[StaffMember]:
    """Return best available resident for this room, or None."""
    candidates = [
        s for s in pool
        if s.name not in used
        and not (room.flagged_fluoro and "no-fluoro" in s.restrictions)
        # R2 only for complex/long cases (Hard Rule 6)
        and not (s.resident_level == "R2" and not room.flagged_complex)
    ]
    if not candidates:
        return None

    def score(s: StaffMember) -> int:
        sc = 0
        if s.name == room.daytime_resident:
            sc += 100
        if s.daytime_or == room.id:
            sc += 80
        # Prefer complex rooms get higher-level residents (soft rule 6)
        if room.flagged_complex and s.resident_level == "R2":
            sc += 40
        # Prefer R3/R4 for routine rooms
        if not room.flagged_complex and s.resident_level in ("R3", "R4"):
            sc += 20
        return sc

    return max(candidates, key=score)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def plan(rooms: List[OperatingRoom], staff: List[StaffMember]) -> CoveragePlan:
    """Run the full 7-step assignment algorithm and return a CoveragePlan."""

    warnings: List[CoverageWarning] = []
    relief_entries: List[ReliefEntry] = []

    # --- pools ----------------------------------------------------------
    late_rooms = [r for r in rooms if r.is_late_running]
    staff_names: Set[str] = {s.name for s in staff if s.available_past_5pm}
    crna_pool = [s for s in staff if s.role == "CRNA" and s.available_past_5pm]
    resident_pool = [
        s for s in staff
        if s.role == "resident" and s.available_past_5pm
    ]
    attending_pool = [s for s in staff if s.role == "attending" and s.available_past_5pm]

    # Physical provider tracking: or_id -> (provider_name, type, is_continuity)
    physical: Dict[int, Tuple[str, str, bool]] = {}
    used_providers: Set[str] = set()

    # ------------------------------------------------------------------ #
    # Step 1 – Identify relief needs                                       #
    # ------------------------------------------------------------------ #
    for room in late_rooms:
        for name, role in [
            (room.daytime_crna, "CRNA"),
            (room.daytime_resident, "resident"),
        ]:
            if name and name not in staff_names:
                relief_entries.append(ReliefEntry(room.id, name, role))
        if room.daytime_attending and room.daytime_attending not in staff_names:
            relief_entries.append(ReliefEntry(room.id, room.daytime_attending, "attending"))

    # ------------------------------------------------------------------ #
    # Step 2 – Place CRNAs                                                 #
    # ------------------------------------------------------------------ #
    # Sort so continuity rooms (daytime CRNA available) are tried first
    rooms_sorted_crna = sorted(
        late_rooms,
        key=lambda r: (
            0 if (r.daytime_crna and r.daytime_crna in staff_names) else 1,
            0 if r.flagged_complex else 1,
        ),
    )

    for room in rooms_sorted_crna:
        if room.id in physical:
            continue
        crna = _find_crna(room, crna_pool, used_providers, staff_names)
        if crna:
            is_cont = crna.name == room.daytime_crna
            physical[room.id] = (crna.name, "CRNA", is_cont)
            used_providers.add(crna.name)

    # ------------------------------------------------------------------ #
    # Step 3 – Place residents (only after CRNA pool exhausted per room)   #
    # ------------------------------------------------------------------ #
    rooms_sorted_res = sorted(
        late_rooms,
        key=lambda r: (
            0 if r.flagged_complex else 1,
            0 if (r.daytime_resident and r.daytime_resident in staff_names) else 1,
        ),
    )

    for room in rooms_sorted_res:
        if room.id in physical:
            continue
        resident = _find_resident(room, resident_pool, used_providers)
        if resident:
            is_cont = resident.name == room.daytime_resident
            physical[room.id] = (resident.name, "resident", is_cont)
            used_providers.add(resident.name)

    # Warn about rooms still lacking a physical provider (will need solo attending)
    rooms_needing_solo: List[OperatingRoom] = []
    for room in late_rooms:
        if room.id not in physical:
            rooms_needing_solo.append(room)

    # ------------------------------------------------------------------ #
    # Steps 4-6 – Assign attendings                                        #
    # ------------------------------------------------------------------ #

    # Build attending state objects
    att_states: Dict[str, _AttState] = {
        a.name: _AttState(staff=a) for a in attending_pool
    }
    used_attendings: Set[str] = set()

    # Rooms with a physical provider need supervisors
    rooms_needing_supervisor = [r for r in late_rooms if r.id in physical]

    # Sort supervised rooms: deployed attendings' continuity rooms first,
    # then by floor/building so geographic clustering emerges naturally
    def _supervised_sort_key(r: OperatingRoom):
        prov_name, prov_type, _ = physical[r.id]
        res_type_order = 0 if prov_type == "resident" else 1  # residents constrain ratio more
        return (r.building, r.floor, res_type_order)

    rooms_needing_supervisor.sort(key=_supervised_sort_key)

    # Sort attendings for supervision: 3p-10p first, then 2-A, 7a-7p, 1-A last
    def _att_supervision_order(a: StaffMember) -> Tuple:
        priority = _SUPERVISION_PRIORITY.get(a.shift_type, 99)
        already = 0 if a.already_deployed else 1
        return (priority, already)

    ordered_attendings = sorted(attending_pool, key=_att_supervision_order)

    # Tracks which supervised rooms already have an attending assigned
    rooms_with_supervisor: Set[int] = set()

    # -- First pass: continuity (2-A/3p-10p already deployed in a room) --
    for att in ordered_attendings:
        if not att.already_deployed or att.daytime_or is None:
            continue
        room = next((r for r in rooms_needing_supervisor if r.id == att.daytime_or), None)
        if room is None:
            continue
        prov_name, prov_type, _ = physical[room.id]
        att_st = att_states[att.name]
        if att_st.can_supervise(room, prov_type, room.is_new_start):
            att_st.add_supervised(room, prov_type, room.is_new_start)
            used_attendings.add(att.name)
            rooms_with_supervisor.add(room.id)

    # -- Main pass: greedily assign each supervised room to best attending --
    for room in rooms_needing_supervisor:
        if room.id in rooms_with_supervisor:
            continue   # already claimed by the continuity pass

        prov_name, prov_type, prov_cont = physical[room.id]

        best_att: Optional[StaffMember] = None
        best_score = -9999

        for att in ordered_attendings:
            att_st = att_states[att.name]
            if not att_st.can_supervise(room, prov_type, room.is_new_start):
                continue
            score = _supervision_score(att_st, room, prov_type)
            if score > best_score:
                best_score = score
                best_att = att

        if best_att:
            att_states[best_att.name].add_supervised(room, prov_type, room.is_new_start)
            used_attendings.add(best_att.name)
            rooms_with_supervisor.add(room.id)
        else:
            warnings.append(CoverageWarning(
                severity="error",
                message=f"OR {room.id}: no available attending can supervise "
                        f"({prov_type} in room — all supervisors at capacity or restricted)",
                or_id=room.id,
            ))

    # -- Steps 5-6: Assign solo attendings to rooms with no physical provider --
    def _att_solo_order(a: StaffMember) -> Tuple:
        priority = _SOLO_PRIORITY.get(a.shift_type, 99)
        return (priority,)

    solo_ordered = sorted(attending_pool, key=_att_solo_order)

    unassigned_rooms: List[int] = []
    for room in rooms_needing_solo:
        best_att = None
        best_score = -9999
        for att in solo_ordered:
            att_st = att_states[att.name]
            if not att_st.can_go_solo(room):
                continue
            score = _solo_score(att_st, room)
            if score > best_score:
                best_score = score
                best_att = att

        if best_att:
            att_states[best_att.name].set_solo(room)
            used_attendings.add(best_att.name)
        else:
            unassigned_rooms.append(room.id)
            warnings.append(CoverageWarning(
                severity="error",
                message=f"OR {room.id}: no available attending for solo coverage — "
                        "consider asking daytime staff to stay",
                or_id=room.id,
            ))

    # ------------------------------------------------------------------ #
    # Step 7 – Optimization passes                                         #
    # ------------------------------------------------------------------ #

    # 7a: Check if any solo room could be converted to supervised
    #     (if there is a free CRNA who could take the room)
    free_crnas = [
        s for s in crna_pool
        if s.name not in used_providers
        and not s.name  # placeholder — logic below is more specific
    ]
    # Rebuild free crna list properly
    free_crnas = [s for s in crna_pool if s.name not in used_providers]
    for room in rooms_needing_solo[:]:
        if room.id in unassigned_rooms:
            continue  # already flagged
        crna = _find_crna(room, free_crnas, used_providers, staff_names)
        if not crna:
            continue
        # Check if any supervisor can take this room if we add a CRNA
        for att in ordered_attendings:
            att_st = att_states[att.name]
            if not att_st.can_supervise(room, "CRNA", room.is_new_start):
                continue
            # Swap: convert solo -> supervised
            att_st.add_supervised(room, "CRNA", room.is_new_start)
            used_providers.add(crna.name)
            physical[room.id] = (crna.name, "CRNA", crna.name == room.daytime_crna)
            # Free up the previously assigned solo attending
            solo_att_name = next(
                (n for n, s in att_states.items() if s.solo_room == room.id), None
            )
            if solo_att_name:
                att_states[solo_att_name].solo_room = None
                att_states[solo_att_name].building = None
                att_states[solo_att_name].floors = set()
            rooms_needing_solo.remove(room)
            warnings.append(CoverageWarning(
                severity="info",
                message=f"OR {room.id}: converted solo-attending room to CRNA-supervised "
                        f"(CRNA {crna.name} added; supervised by {att.name})",
                or_id=room.id,
            ))
            break

    # 7b: Check 1-A reserve capacity
    one_a_attendings = [a for a in attending_pool if a.shift_type == "1-A"]
    for att in one_a_attendings:
        att_st = att_states[att.name]
        total = att_st.total_supervised + (1 if att_st.is_solo else 0)
        if total >= 4:
            warnings.append(CoverageWarning(
                severity="warning",
                message=f"1-A attending {att.name} has {total} room(s) — "
                        "no reserve capacity for emergent cases",
                or_id=None,
            ))

    # 7c: Verify no cross-building supervision
    for att in attending_pool:
        att_st = att_states[att.name]
        has_legacy = any(f in ("THOR", "Gray", "Jackson") for f in att_st.floors)
        has_lunder = any(f in ("L2", "L3", "L4") for f in att_st.floors)
        if has_legacy and has_lunder:
            warnings.append(CoverageWarning(
                severity="error",
                message=f"VIOLATION: {att.name} is supervising across Legacy and Lunder — "
                        "must be split",
                or_id=None,
            ))

    # 7d: Verify Lunder floor constraint for all supervisors
    for att in attending_pool:
        att_st = att_states[att.name]
        lunder_floors = att_st.floors & {"L2", "L3", "L4"}
        if not floors_compatible(lunder_floors):
            warnings.append(CoverageWarning(
                severity="error",
                message=f"VIOLATION: {att.name} supervises L2 + L4 without L3",
                or_id=None,
            ))

    # ------------------------------------------------------------------ #
    # Build output                                                         #
    # ------------------------------------------------------------------ #

    # Map or_id -> attending from att_states
    or_to_attending: Dict[int, Tuple[str, str]] = {}   # or_id -> (name, role)
    for att_name, att_st in att_states.items():
        for or_id in att_st.supervised_rooms:
            or_to_attending[or_id] = (att_name, "supervisor")
        if att_st.solo_room is not None:
            or_to_attending[att_st.solo_room] = (att_name, "solo")

    assignments: List[Assignment] = []
    no_physical_rooms: List[int] = []

    for room in late_rooms:
        att_info = or_to_attending.get(room.id)
        if att_info is None:
            # Unassigned — record separately
            if room.id not in unassigned_rooms:
                unassigned_rooms.append(room.id)
            continue

        att_name, att_role = att_info
        phys_info = physical.get(room.id)
        if phys_info:
            prov_name, prov_type, prov_cont = phys_info
        else:
            prov_name, prov_type, prov_cont = None, None, False
            if att_role == "solo":
                prov_cont = False

        # Continuity: true if physical provider was daytime provider of this room
        continuity = prov_cont
        if att_role == "solo":
            # Check if the solo attending was the daytime attending
            continuity = (att_name == room.daytime_attending)

        if phys_info is None and att_role != "solo":
            no_physical_rooms.append(room.id)

        assignments.append(Assignment(
            or_id=room.id,
            attending=att_name,
            attending_role=att_role,
            physical_provider=prov_name if att_role != "solo" else None,
            physical_provider_type=prov_type if att_role != "solo" else None,
            continuity=continuity,
        ))

    # Build supervisor groups summary
    supervisor_groups: Dict[str, List[int]] = defaultdict(list)
    for a_name, att_st in att_states.items():
        if att_st.supervised_rooms:
            supervisor_groups[a_name] = list(att_st.supervised_rooms)
        elif att_st.solo_room is not None:
            supervisor_groups[a_name] = [att_st.solo_room]

    # Soft preference warnings
    _soft_preference_warnings(attending_pool, att_states, late_rooms, physical, warnings)

    return CoveragePlan(
        assignments=assignments,
        unassigned_rooms=unassigned_rooms,
        no_physical_rooms=no_physical_rooms,
        warnings=warnings,
        relief_entries=relief_entries,
        supervisor_groups=dict(supervisor_groups),
    )


def _soft_preference_warnings(
    attending_pool: List[StaffMember],
    att_states: Dict[str, _AttState],
    late_rooms: List[OperatingRoom],
    physical: Dict,
    warnings: List[CoverageWarning],
) -> None:
    """Emit info-level warnings for soft-preference deviations."""

    # Soft 1: check if any CRNA slots remain unused while rooms have residents
    crna_supervised = sum(
        1 for or_id, (_, ptype, _) in physical.items() if ptype == "CRNA"
    )
    resident_supervised = sum(
        1 for or_id, (_, ptype, _) in physical.items() if ptype == "resident"
    )
    if resident_supervised > 0 and crna_supervised > 0:
        pass  # normal mixed usage, no warning needed

    # Soft 3: 1-A attending management
    for att in attending_pool:
        if att.shift_type != "1-A":
            continue
        att_st = att_states[att.name]
        if att_st.total_supervised >= 3:
            warnings.append(CoverageWarning(
                severity="warning",
                message=f"1-A {att.name} is supervising {att_st.total_supervised} rooms — "
                        "consider redistributing to preserve emergency reserve",
            ))

    # Soft 5: long cases (past 8pm) should go to 1-A, 2-A, or 3p-10p
    late_shifts = {"1-A", "2-A", "3p-10p"}
    for room in late_rooms:
        end = room.estimated_end or ""
        if any(kw in end.lower() for kw in ("midnight", "10pm", "11pm", "9pm")):
            phys_info = physical.get(room.id)
            # Find who's covering this room
            from .zones import get_floor as _gf
            for a_name, att_st in att_states.items():
                if room.id in att_st.supervised_rooms or att_st.solo_room == room.id:
                    att_obj = next((a for a in attending_pool if a.name == a_name), None)
                    if att_obj and att_obj.shift_type not in late_shifts:
                        warnings.append(CoverageWarning(
                            severity="warning",
                            message=f"OR {room.id} runs until {end} but is covered by "
                                    f"{a_name} ({att_obj.shift_type}) — "
                                    "prefer 1-A / 2-A / 3p-10p to minimise handoffs",
                            or_id=room.id,
                        ))
