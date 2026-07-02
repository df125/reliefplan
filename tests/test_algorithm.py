"""Regression tests for the coverage algorithm.

Two fixtures:
  TestSampleInput   — stable attending assignments across the algorithm refactor
  TestWhiteDzuroL4  — region-first logic gives White-Dzuro 3:1 CRNA on L4
"""
import pytest

from reliefplan.algorithm import plan
from reliefplan.loader import _parse_room, _parse_staff
from reliefplan.models import OperatingRoom, StaffMember

# ---------------------------------------------------------------------------
# Helper: build rooms/staff from the CLI sample input dict
# ---------------------------------------------------------------------------

def _sample_rooms_and_staff():
    from reliefplan.cli import SAMPLE_INPUT
    rooms = [_parse_room(r, i) for i, r in enumerate(SAMPLE_INPUT["rooms"])]
    staff = [_parse_staff(s, i) for i, s in enumerate(SAMPLE_INPUT["staff"])]
    return rooms, staff


# ---------------------------------------------------------------------------
# Fixture 1 – sample input
# ---------------------------------------------------------------------------

class TestSampleInput:
    """Attending-per-room assignments must remain identical after the refactor."""

    @pytest.fixture(autouse=True)
    def run_plan(self):
        rooms, staff = _sample_rooms_and_staff()
        self.result = plan(rooms, staff)

    def test_attending_assignments(self):
        assignments = {a.or_id: a.attending for a in self.result.assignments}
        assert assignments[44] == "Dr. Torres"
        assert assignments[5] == "Dr. Torres"
        assert assignments[15] == "Dr. Johnson"
        assert assignments[63] == "Dr. Williams"
        assert assignments[71] == "Dr. Williams"
        assert assignments[82] == "Dr. Brown"

    def test_no_unassigned_rooms(self):
        assert self.result.unassigned_rooms == []

    def test_or15_gets_crna(self):
        """Fluoro room 15 should get a CRNA (Santos has no-fluoro, so Chen or similar)."""
        a = next(a for a in self.result.assignments if a.or_id == 15)
        assert a.physical_provider_type == "CRNA"

    def test_or82_gets_r2_resident(self):
        """Complex room 82 has no available CRNA continuity (Park absent) → R2 resident."""
        a = next(a for a in self.result.assignments if a.or_id == 82)
        assert a.physical_provider_type == "resident"
        assert a.physical_provider == "Dr. Nguyen"

    def test_or71_gets_r3_resident(self):
        """Non-complex OR 71 gets an R3 resident (Dr. Nguyen is reserved for complex OR 82)."""
        a = next(a for a in self.result.assignments if a.or_id == 71)
        assert a.physical_provider_type == "resident"
        assert a.physical_provider == "Dr. Kim"


# ---------------------------------------------------------------------------
# Fixture 2 – White-Dzuro L4 scenario
# ---------------------------------------------------------------------------

class TestWhiteDzuroL4:
    """
    White-Dzuro is locked to OR 90 on L4.  Three L4 rooms, three CRNAs, one
    unrelated evening resident (Jenkins, R3).  The algorithm should recognise
    the full L4 CRNA cluster and assign all three rooms CRNAs so White-Dzuro
    can supervise 3:1.  Jenkins should not be placed.
    """

    @pytest.fixture(autouse=True)
    def run_plan(self):
        rooms = [
            OperatingRoom(id=83, building="Lunder", floor="L4"),
            OperatingRoom(id=86, building="Lunder", floor="L4"),
            OperatingRoom(id=90, building="Lunder", floor="L4"),
        ]
        staff = [
            StaffMember(
                name="White-Dzuro", role="attending", shift_type="3p-10p",
                available_past_5pm=True, already_deployed=True, daytime_or=90,
            ),
            StaffMember(name="CRNA_A", role="CRNA", shift_type="CRNA-5p-8p",
                        available_past_5pm=True),
            StaffMember(name="CRNA_B", role="CRNA", shift_type="CRNA-5p-8p",
                        available_past_5pm=True),
            StaffMember(name="CRNA_C", role="CRNA", shift_type="CRNA-5p-8p",
                        available_past_5pm=True),
            StaffMember(name="Jenkins", role="resident", resident_level="R3",
                        shift_type="3p-10p", available_past_5pm=True),
        ]
        self.result = plan(rooms, staff)

    def test_all_rooms_assigned(self):
        assert len(self.result.assignments) == 3
        assert self.result.unassigned_rooms == []

    def test_all_l4_rooms_get_crnas(self):
        for a in self.result.assignments:
            assert a.physical_provider_type == "CRNA", (
                f"OR {a.or_id} got {a.physical_provider_type!r} instead of 'CRNA'"
            )

    def test_white_dzuro_supervises_3_rooms(self):
        wd_rooms = [a.or_id for a in self.result.assignments if a.attending == "White-Dzuro"]
        assert len(wd_rooms) == 3

    def test_jenkins_not_placed(self):
        providers = [a.physical_provider for a in self.result.assignments]
        assert "Jenkins" not in providers


# ---------------------------------------------------------------------------
# Structural invariants — independent of specific names
# ---------------------------------------------------------------------------

class TestInvariants:
    """Structural invariants of any plan over the sample input."""

    @pytest.fixture(autouse=True)
    def run_plan(self):
        self.rooms, self.staff = _sample_rooms_and_staff()
        self.rooms_by_id = {r.id: r for r in self.rooms}
        self.result = plan(self.rooms, self.staff)

    def _rooms_by_attending(self):
        grouped: dict[str, list] = {}
        for a in self.result.assignments:
            grouped.setdefault(a.attending, []).append(self.rooms_by_id[a.or_id])
        return grouped

    def test_supervisor_floors_compatible(self):
        """Each attending's room set spans compatible floors and one building."""
        from reliefplan.zones import floors_compatible
        for att, rooms in self._rooms_by_attending().items():
            floors = {r.floor for r in rooms}
            assert floors_compatible(floors), (
                f"{att} covers incompatible floors {sorted(floors)}"
            )
            buildings = {r.building for r in rooms}
            assert len(buildings) == 1, (
                f"{att} covers multiple buildings {sorted(buildings)}"
            )

    def test_ratio_limits(self):
        """Mirror _AttState.can_supervise: any resident → max 2 rooms;
        all-CRNA supervision → max 4 rooms; solo attendings cover 1 room only."""
        by_att: dict[str, list] = {}
        for a in self.result.assignments:
            by_att.setdefault(a.attending, []).append(a)
        for att, assignments in by_att.items():
            solo = [a for a in assignments if a.attending_role == "solo"]
            if solo:
                assert len(assignments) == 1, (
                    f"{att} is solo in OR {solo[0].or_id} but covers "
                    f"{len(assignments)} rooms"
                )
                continue
            resident_count = sum(
                1 for a in assignments if a.physical_provider_type == "resident"
            )
            total = len(assignments)
            if resident_count > 0:
                assert total <= 2, (
                    f"{att} supervises {total} rooms with a resident (max 2)"
                )
            else:
                assert total <= 4, (
                    f"{att} supervises {total} CRNA rooms (max 4)"
                )

    def test_physical_providers_unique(self):
        """Each staff member is physical provider in at most one room."""
        providers = [
            a.physical_provider for a in self.result.assignments
            if a.physical_provider
        ]
        assert len(providers) == len(set(providers)), (
            f"Duplicate physical providers: {providers}"
        )

    def test_every_late_room_accounted_for_exactly_once(self):
        """Every late room appears in exactly one of assignments/unassigned."""
        late_ids = {r.id for r in self.rooms if r.is_late_running}
        assigned_ids = [a.or_id for a in self.result.assignments]
        assert len(assigned_ids) == len(set(assigned_ids)), (
            f"Room assigned more than once: {assigned_ids}"
        )
        unassigned_ids = self.result.unassigned_rooms
        assert set(assigned_ids).isdisjoint(unassigned_ids)
        assert set(assigned_ids) | set(unassigned_ids) == late_ids
