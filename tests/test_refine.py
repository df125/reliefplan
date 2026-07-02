"""Unit tests for reliefplan.refine.apply_edits."""
import pytest

from reliefplan.loader import _parse_room, _parse_staff
from reliefplan.refine import apply_edits

# ---------------------------------------------------------------------------
# Fixtures: small rooms/staff set + a base plan dict (shape of /api/plan output)
# ---------------------------------------------------------------------------

_ROOM_DICTS = [
    # Legacy / THOR, complex
    {"id": 44, "building": "Legacy", "floor": "THOR",
     "daytimeAttending": "Dr. Patel", "flaggedComplex": True},
    # Legacy / THOR, fluoro-flagged
    {"id": 15, "building": "Legacy", "floor": "THOR",
     "daytimeAttending": "Dr. Rivera", "flaggedFluoro": True},
    # Lunder / L3
    {"id": 63, "building": "Lunder", "floor": "L3",
     "daytimeAttending": "Dr. Chen"},
    # Lunder / L3, initially unassigned in the base plan
    {"id": 71, "building": "Lunder", "floor": "L3",
     "daytimeAttending": "Dr. Chen"},
]

_STAFF_DICTS = [
    {"name": "Dr. Johnson", "role": "attending", "shiftType": "1-A"},
    {"name": "Dr. Torres", "role": "attending", "shiftType": "7a-7p",
     "restrictions": ["no-fluoro"]},
    {"name": "Dr. Williams", "role": "attending", "shiftType": "3p-10p"},
    {"name": "Chen", "role": "CRNA", "shiftType": "CRNA-5p-8p"},
    {"name": "Santos", "role": "CRNA", "shiftType": "CRNA-7a-8p",
     "restrictions": ["no-fluoro"]},
    {"name": "Dr. Kim", "role": "resident", "residentLevel": "R3",
     "shiftType": "3p-10p"},
]


@pytest.fixture
def rooms():
    return [_parse_room(r, i) for i, r in enumerate(_ROOM_DICTS)]


@pytest.fixture
def staff():
    return [_parse_staff(s, i) for i, s in enumerate(_STAFF_DICTS)]


def _assignment(or_id, attending, role="solo", physical=None, physical_type=None):
    return {
        "or_id": or_id,
        "attending": attending,
        "attending_role": role,
        "physical_provider": physical,
        "physical_provider_type": physical_type,
        "continuity": False,
    }


@pytest.fixture
def base_plan():
    """OR 44: Torres solo. OR 15: Johnson + Chen (CRNA). OR 63: Williams solo.
    OR 71: unassigned."""
    return {
        "assignments": [
            _assignment(44, "Dr. Torres"),
            _assignment(15, "Dr. Johnson", role="supervisor",
                        physical="Chen", physical_type="CRNA"),
            _assignment(63, "Dr. Williams"),
        ],
        "unassigned_rooms": [71],
        "no_physical_rooms": [44, 63],
        "warnings": [],
        "relief_entries": [],
        "supervisor_groups": {},
    }


def _by_or(plan):
    return {a["or_id"]: a for a in plan["assignments"]}


# ---------------------------------------------------------------------------
# set_attending
# ---------------------------------------------------------------------------

class TestSetAttending:
    def test_new_assignment_on_unassigned_room(self, base_plan, rooms, staff):
        edits = [{"operation": "set_attending", "or_id": 71, "name": "Dr. Williams"}]
        plan, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert rejected == []
        a = _by_or(plan)[71]
        assert a["attending"] == "Dr. Williams"
        assert a["attending_role"] == "solo"  # no physical provider
        assert 71 not in plan["unassigned_rooms"]
        assert applied == ["OR 71: attending set to Dr. Williams"]

    def test_change_existing_attending(self, base_plan, rooms, staff):
        edits = [{"operation": "set_attending", "or_id": 44, "name": "Dr. Johnson"}]
        plan, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert rejected == []
        assert _by_or(plan)[44]["attending"] == "Dr. Johnson"
        assert applied == ["OR 44: attending changed from Dr. Torres → Dr. Johnson"]

    def test_role_stays_supervisor_when_physical_present(self, base_plan, rooms, staff):
        # Free Williams from his Lunder room first — hard-rule validation
        # forbids covering Lunder OR 63 and Legacy OR 15 simultaneously
        edits = [
            {"operation": "remove_attending", "or_id": 63},
            {"operation": "set_attending", "or_id": 15, "name": "Dr. Williams"},
        ]
        plan, _, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert rejected == []
        a = _by_or(plan)[15]
        assert a["attending"] == "Dr. Williams"
        assert a["attending_role"] == "supervisor"

    def test_cross_building_assignment_rejected(self, base_plan, rooms, staff):
        # Williams covers Lunder OR 63; adding Legacy OR 15 must be rejected
        edits = [{"operation": "set_attending", "or_id": 15, "name": "Dr. Williams"}]
        plan, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert len(rejected) == 1 and "hard rule" in rejected[0]
        assert _by_or(plan)[15]["attending"] == "Dr. Johnson"

    def test_unknown_staff_name_rejected(self, base_plan, rooms, staff):
        edits = [{"operation": "set_attending", "or_id": 44, "name": "Dr. Nobody"}]
        plan, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == ["'Dr. Nobody' not found in staff list."]
        # Original attending untouched
        assert _by_or(plan)[44]["attending"] == "Dr. Torres"

    def test_non_attending_rejected(self, base_plan, rooms, staff):
        edits = [{"operation": "set_attending", "or_id": 44, "name": "Chen"}]
        _, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == ["'Chen' is a CRNA, not an attending."]

    def test_no_fluoro_attending_rejected_for_fluoro_room(self, base_plan, rooms, staff):
        edits = [{"operation": "set_attending", "or_id": 15, "name": "Dr. Torres"}]
        plan, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == [
            "'Dr. Torres' has a no-fluoro restriction; OR 15 requires fluoro capability."
        ]
        assert _by_or(plan)[15]["attending"] == "Dr. Johnson"


# ---------------------------------------------------------------------------
# set_physical
# ---------------------------------------------------------------------------

class TestSetPhysical:
    def test_set_crna(self, base_plan, rooms, staff):
        edits = [{"operation": "set_physical", "or_id": 44, "name": "Chen"}]
        plan, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert rejected == []
        a = _by_or(plan)[44]
        assert a["physical_provider"] == "Chen"
        assert a["physical_provider_type"] == "CRNA"
        assert a["attending_role"] == "supervisor"
        assert applied == ["OR 44: physical provider set to Chen (CRNA)"]
        assert 44 not in plan["no_physical_rooms"]

    def test_set_resident(self, base_plan, rooms, staff):
        edits = [{"operation": "set_physical", "or_id": 63, "name": "Dr. Kim"}]
        plan, _, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert rejected == []
        a = _by_or(plan)[63]
        assert a["physical_provider"] == "Dr. Kim"
        assert a["physical_provider_type"] == "resident"
        assert a["attending_role"] == "supervisor"

    def test_attending_as_physical_rejected(self, base_plan, rooms, staff):
        edits = [{"operation": "set_physical", "or_id": 44, "name": "Dr. Williams"}]
        _, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == [
            "'Dr. Williams' is an attending — use 'set_attending' to change the attending."
        ]

    def test_fluoro_restriction_rejected(self, base_plan, rooms, staff):
        edits = [{"operation": "set_physical", "or_id": 15, "name": "Santos"}]
        plan, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == [
            "'Santos' has a no-fluoro restriction; OR 15 requires fluoro capability."
        ]
        assert _by_or(plan)[15]["physical_provider"] == "Chen"

    def test_room_without_attending_rejected(self, base_plan, rooms, staff):
        edits = [{"operation": "set_physical", "or_id": 71, "name": "Chen"}]
        _, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == [
            "OR 71 has no attending assigned. "
            "Assign an attending before adding a physical provider."
        ]

    def test_unknown_name_rejected(self, base_plan, rooms, staff):
        edits = [{"operation": "set_physical", "or_id": 44, "name": "Ghost"}]
        _, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == ["'Ghost' not found in staff list."]


# ---------------------------------------------------------------------------
# swap_attendings
# ---------------------------------------------------------------------------

class TestSwapAttendings:
    def test_swap_both_assigned(self, base_plan, rooms, staff):
        edits = [{"operation": "swap_attendings", "or_id": 44, "or_id_2": 63}]
        plan, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert rejected == []
        assignments = _by_or(plan)
        assert assignments[44]["attending"] == "Dr. Williams"
        assert assignments[63]["attending"] == "Dr. Torres"
        assert len(applied) == 1 and "swapped" in applied[0]

    def test_swap_with_unassigned_side(self, base_plan, rooms, staff):
        edits = [{"operation": "swap_attendings", "or_id": 63, "or_id_2": 71}]
        plan, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert rejected == []
        assignments = _by_or(plan)
        # Williams moved to previously-unassigned OR 71
        assert assignments[71]["attending"] == "Dr. Williams"
        assert 71 not in plan["unassigned_rooms"]
        # OR 63 was vacated: dropped from assignments and marked unassigned
        assert 63 not in assignments
        assert 63 in plan["unassigned_rooms"]
        assert len(applied) == 1 and "swapped" in applied[0]

    def test_swap_missing_second_or_rejected(self, base_plan, rooms, staff):
        edits = [{"operation": "swap_attendings", "or_id": 44}]
        _, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == ["swap_attendings requires two OR numbers."]

    def test_swap_both_unassigned_rejected(self, base_plan, rooms, staff):
        edits = [{"operation": "swap_attendings", "or_id": 71, "or_id_2": 99}]
        _, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == ["Neither OR 71 nor OR 99 has an attending assigned."]


# ---------------------------------------------------------------------------
# remove_attending / remove_physical
# ---------------------------------------------------------------------------

class TestRemoveOps:
    def test_remove_attending(self, base_plan, rooms, staff):
        edits = [{"operation": "remove_attending", "or_id": 63}]
        plan, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert rejected == []
        assert 63 not in _by_or(plan)
        assert 63 in plan["unassigned_rooms"]
        assert applied == ["OR 63: attending removed (now unassigned)."]

    def test_remove_attending_from_unassigned_rejected(self, base_plan, rooms, staff):
        edits = [{"operation": "remove_attending", "or_id": 71}]
        _, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == ["OR 71 had no attending — nothing to remove."]

    def test_remove_physical(self, base_plan, rooms, staff):
        edits = [{"operation": "remove_physical", "or_id": 15}]
        plan, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert rejected == []
        a = _by_or(plan)[15]
        assert a["physical_provider"] is None
        assert a["physical_provider_type"] is None
        assert a["attending_role"] == "solo"
        assert 15 in plan["no_physical_rooms"]
        assert applied == ["OR 15: Chen removed — attending now covers solo."]

    def test_remove_physical_when_none_rejected(self, base_plan, rooms, staff):
        edits = [{"operation": "remove_physical", "or_id": 44}]
        _, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == ["OR 44 already has no physical provider."]


# ---------------------------------------------------------------------------
# Miscellaneous
# ---------------------------------------------------------------------------

class TestMisc:
    def test_unknown_operation_rejected(self, base_plan, rooms, staff):
        edits = [{"operation": "teleport", "or_id": 44}]
        _, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == ["Unknown operation 'teleport'."]

    def test_llm_prerejected_edit_passes_reason_through(self, base_plan, rooms, staff):
        edits = [{
            "operation": "set_attending", "or_id": 44, "name": "Dr. Johnson",
            "rejected": True,
            "rejection_reason": "Cross-building supervision is not allowed.",
        }]
        plan, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == ["Cross-building supervision is not allowed."]
        # Edit must not have been applied
        assert _by_or(plan)[44]["attending"] == "Dr. Torres"

    def test_llm_prerejected_without_reason_gets_default(self, base_plan, rooms, staff):
        edits = [{"operation": "set_physical", "or_id": 63, "rejected": True}]
        _, applied, rejected = apply_edits(base_plan, edits, rooms, staff)
        assert applied == []
        assert rejected == ["Cannot apply set_physical to OR 63."]

    def test_input_plan_not_mutated(self, base_plan, rooms, staff):
        edits = [{"operation": "remove_attending", "or_id": 44}]
        apply_edits(base_plan, edits, rooms, staff)
        assert {a["or_id"] for a in base_plan["assignments"]} == {44, 15, 63}
        assert base_plan["unassigned_rooms"] == [71]

    def test_derived_fields_rebuilt(self, base_plan, rooms, staff):
        edits = [{"operation": "set_physical", "or_id": 44, "name": "Chen"}]
        plan, _, _ = apply_edits(base_plan, edits, rooms, staff)
        assert plan["supervisor_groups"] == {
            "Dr. Torres": [44],
            "Dr. Johnson": [15],
            "Dr. Williams": [63],
        }
        assert plan["no_physical_rooms"] == [63]
        assert plan["unassigned_rooms"] == [71]
