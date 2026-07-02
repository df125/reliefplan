"""Apply LLM-generated edit operations to a coverage plan dict."""
from __future__ import annotations

import copy
from typing import Any


def apply_edits(
    plan: dict[str, Any],
    edits: list[dict[str, Any]],
    rooms: list,   # list[OperatingRoom]
    staff: list,   # list[StaffMember]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """
    Apply structured edits to a plan dict (as returned by /api/plan).
    Returns (updated_plan, applied_messages, rejected_messages).
    """
    plan = copy.deepcopy(plan)

    rooms_by_id   = {r.id: r for r in rooms}
    staff_by_name = {s.name: s for s in staff}

    assignments: dict[int, dict] = {a["or_id"]: a for a in plan.get("assignments", [])}
    unassigned: set[int]         = set(plan.get("unassigned_rooms", []))

    applied:  list[str] = []
    rejected: list[str] = []

    for edit in edits:
        op     = edit.get("operation", "")
        or_id  = edit.get("or_id")
        or_id2 = edit.get("or_id_2")
        name   = edit.get("name")

        # LLM already determined this edit violates a rule
        if edit.get("rejected"):
            reason = edit.get("rejection_reason") or f"Cannot apply {op} to OR {or_id}."
            rejected.append(reason)
            continue

        # ── set_attending ──────────────────────────────────────────────────
        if op == "set_attending":
            err = _validate_attending(name, or_id, staff_by_name, rooms_by_id)
            if err:
                rejected.append(err)
                continue
            old = (assignments[or_id]["attending"] if or_id in assignments else None)
            if or_id not in assignments:
                assignments[or_id] = _new_assignment(or_id)
                unassigned.discard(or_id)
            assignments[or_id]["attending"] = name
            # Infer role: solo if no physical provider
            phys = assignments[or_id].get("physical_provider")
            assignments[or_id]["attending_role"] = "supervisor" if phys else "solo"
            msg = f"OR {or_id}: attending set to {name}"
            if old and old != name:
                msg = f"OR {or_id}: attending changed from {old} → {name}"
            applied.append(msg)

        # ── set_physical ───────────────────────────────────────────────────
        elif op == "set_physical":
            if not name or name not in staff_by_name:
                rejected.append(f"'{name}' not found in staff list.")
                continue
            sm = staff_by_name[name]
            if sm.role not in ("CRNA", "resident"):
                rejected.append(
                    f"'{name}' is an attending — use 'set_attending' to change the attending."
                )
                continue
            if or_id not in assignments:
                rejected.append(
                    f"OR {or_id} has no attending assigned. Assign an attending before adding a physical provider."
                )
                continue
            room = rooms_by_id.get(or_id)
            if room and "no-fluoro" in sm.restrictions and room.flagged_fluoro:
                rejected.append(
                    f"'{name}' has a no-fluoro restriction; OR {or_id} requires fluoro capability."
                )
                continue
            old = assignments[or_id].get("physical_provider")
            assignments[or_id]["physical_provider"]      = name
            assignments[or_id]["physical_provider_type"] = sm.role
            assignments[or_id]["attending_role"]         = "supervisor"
            msg = f"OR {or_id}: physical provider set to {name} ({sm.role})"
            if old and old != name:
                msg = f"OR {or_id}: physical provider changed from {old} → {name} ({sm.role})"
            applied.append(msg)

        # ── remove_attending ───────────────────────────────────────────────
        elif op == "remove_attending":
            if or_id not in assignments:
                rejected.append(f"OR {or_id} had no attending — nothing to remove.")
                continue
            del assignments[or_id]
            unassigned.add(or_id)
            applied.append(f"OR {or_id}: attending removed (now unassigned).")

        # ── remove_physical ────────────────────────────────────────────────
        elif op == "remove_physical":
            if or_id not in assignments:
                rejected.append(f"OR {or_id} has no assignment.")
                continue
            old = assignments[or_id].get("physical_provider")
            if not old:
                rejected.append(f"OR {or_id} already has no physical provider.")
                continue
            assignments[or_id]["physical_provider"]      = None
            assignments[or_id]["physical_provider_type"] = None
            assignments[or_id]["attending_role"]         = "solo"
            applied.append(f"OR {or_id}: {old} removed — attending now covers solo.")

        # ── swap_attendings ────────────────────────────────────────────────
        elif op == "swap_attendings":
            if not or_id2:
                rejected.append("swap_attendings requires two OR numbers.")
                continue
            a1 = assignments.get(or_id)
            a2 = assignments.get(or_id2)
            att1 = a1["attending"] if a1 else None
            att2 = a2["attending"] if a2 else None

            if att1 is None and att2 is None:
                rejected.append(f"Neither OR {or_id} nor OR {or_id2} has an attending assigned.")
                continue

            # Validate each attending against their new room
            if att1:
                err = _validate_attending(att1, or_id2, staff_by_name, rooms_by_id)
                if err:
                    rejected.append(f"Cannot move {att1} to OR {or_id2}: {err}")
                    continue
            if att2:
                err = _validate_attending(att2, or_id, staff_by_name, rooms_by_id)
                if err:
                    rejected.append(f"Cannot move {att2} to OR {or_id}: {err}")
                    continue

            if a1: a1["attending"] = att2
            if a2: a2["attending"] = att1

            # Handle one side being unassigned
            if att1 and not a2:
                assignments[or_id2] = _new_assignment(or_id2)
                assignments[or_id2]["attending"] = att1
                unassigned.discard(or_id2)
            if att2 and not a1:
                assignments[or_id] = _new_assignment(or_id)
                assignments[or_id]["attending"] = att2
                unassigned.discard(or_id)
            if not att1 and a1:
                del assignments[or_id]
                unassigned.add(or_id)
            if not att2 and a2:
                del assignments[or_id2]
                unassigned.add(or_id2)

            applied.append(
                f"OR {or_id} ↔ OR {or_id2}: attendings swapped "
                f"({att1 or 'unassigned'} ↔ {att2 or 'unassigned'})."
            )

        else:
            rejected.append(f"Unknown operation '{op}'.")

    # Rebuild derived fields
    supervisor_groups: dict[str, list[int]] = {}
    no_physical: list[int] = []
    for a in assignments.values():
        att = a.get("attending")
        if att:
            supervisor_groups.setdefault(att, []).append(a["or_id"])
        if att and not a.get("physical_provider"):
            no_physical.append(a["or_id"])

    plan["assignments"]       = list(assignments.values())
    plan["unassigned_rooms"]  = sorted(unassigned)
    plan["no_physical_rooms"] = sorted(no_physical)
    plan["supervisor_groups"] = supervisor_groups
    return plan, applied, rejected


def _new_assignment(or_id: int) -> dict:
    return {
        "or_id": or_id,
        "attending": None,
        "attending_role": "solo",
        "physical_provider": None,
        "physical_provider_type": None,
        "continuity": False,
    }


def _validate_attending(
    name: str | None,
    or_id: int,
    staff_by_name: dict,
    rooms_by_id: dict,
) -> str | None:
    """Return an error string if the attending cannot be assigned to or_id, else None."""
    if not name or name not in staff_by_name:
        return f"'{name}' not found in staff list."
    sm = staff_by_name[name]
    if sm.role != "attending":
        return f"'{name}' is a {sm.role}, not an attending."
    room = rooms_by_id.get(or_id)
    if room and "no-fluoro" in sm.restrictions and room.flagged_fluoro:
        return f"'{name}' has a no-fluoro restriction; OR {or_id} requires fluoro capability."
    return None
