"""
LLM-based text parsing for OR schedule and after-5pm staff lists.

Uses Claude with tool use to reliably extract structured data from
free-form pasted text regardless of format or column order.
Falls back gracefully if the API key is absent.
"""
from __future__ import annotations

import os
from typing import Any

import anthropic

_SCHEDULE_TOOL: dict[str, Any] = {
    "name": "extract_schedule",
    "description": (
        "Extract operating room assignments from a pasted anesthesia schedule. "
        "The text may be tab-separated from a spreadsheet, from Epic or another "
        "hospital scheduling system, or typed manually."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "rooms": {
                "type": "array",
                "description": "One entry per operating room found in the text.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "integer",
                            "description": "OR number (1–99). Strip any 'OR' prefix.",
                        },
                        "daytimeAttending": {
                            "type": ["string", "null"],
                            "description": "Attending anesthesiologist name. Null if not listed.",
                        },
                        "daytimeCRNA": {
                            "type": ["string", "null"],
                            "description": "CRNA or AA name. Null if not listed.",
                        },
                        "daytimeResident": {
                            "type": ["string", "null"],
                            "description": "Resident or fellow name. Null if not listed.",
                        },
                    },
                    "required": ["id"],
                },
            }
        },
        "required": ["rooms"],
    },
}

_STAFF_TOOL: dict[str, Any] = {
    "name": "extract_staff",
    "description": (
        "Extract after-5pm anesthesia staff from a pasted call schedule or staff list. "
        "The text may be tab-separated, have section headers like 'Attendings:' or 'CRNAs:', "
        "or be typed as a simple list."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "staff": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Full name as it appears in the text.",
                        },
                        "role": {
                            "type": "string",
                            "enum": ["attending", "CRNA", "resident"],
                        },
                        "shiftType": {
                            "type": "string",
                            "enum": ["1-A", "2-A", "7a-7p", "3p-10p", "CRNA-7a-8p", "CRNA-5p-8p"],
                            "description": (
                                "1-A = first call/overnight attending. "
                                "2-A = second call attending. "
                                "7a-7p = daytime attending staying late. "
                                "3p-10p = afternoon/evening attending. "
                                "CRNA-7a-8p = daytime CRNA staying until 8pm. "
                                "CRNA-5p-8p = evening CRNA starting at 5pm."
                            ),
                        },
                        "residentLevel": {
                            "type": ["string", "null"],
                            "enum": ["R2", "R3", "R4", None],
                            "description": "Only for residents. CA-1=R2, CA-2=R3, CA-3=R4.",
                        },
                        "daytimeOR": {
                            "type": ["integer", "null"],
                            "description": "OR number they covered during the day, if mentioned.",
                        },
                        "alreadyDeployed": {
                            "type": "boolean",
                            "description": (
                                "True if already placed in a room before 5pm "
                                "(indicated by 'in room', 'deployed', 'OR ##', parenthetical OR, etc.)."
                            ),
                        },
                        "restrictions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "e.g. ['no-fluoro'] for pregnant staff. Usually empty.",
                        },
                    },
                    "required": ["name", "role", "shiftType"],
                },
            }
        },
        "required": ["staff"],
    },
}

_SYSTEM_SCHEDULE = """\
You are a data-extraction assistant for the MGH (Massachusetts General Hospital) \
Anesthesia Department. Extract OR room assignments from a pasted daily schedule.

The schedule uses a TRANSPOSED layout — OR numbers are COLUMNS, not rows:
- A section-header row starts with a floor/area name (e.g. "THOR", "Legacy GenSurg Gray", \
"Legacy GenSurg Jackson", "Lunder 2 GenSurg", "Lunder 3 Division", "Lunder 4 Vasc Neuro Rad") \
followed by tab-separated OR numbers.
- Continuation rows within the same section start with whitespace/empty first column \
followed by more OR numbers.
- The next three rows always contain names aligned to the OR number columns:
    "Staff"    → attending anesthesiologist for each OR
    "CRNA"     → CRNA for each OR (may be blank)
    "Resident" → resident for each OR (may be blank)
- "Team Lead:" and "Resource:" lines mark the end of a section — ignore them.
- An empty cell means no one is assigned in that role for that OR.
- "Tutor, Tutee" is a teaching-pair PLACEHOLDER, NOT a real person — treat as null.
- Names appear as "Last, First M" or "Last, First Middle" — keep exactly as shown.
- OR numbers range 1–99; ignore any text that is not a 1–2 digit integer in the OR-number row.

Return one entry per OR number found, with daytimeAttending/daytimeCRNA/daytimeResident \
set to null when the cell is empty or contains "Tutor, Tutee"."""

_SYSTEM_STAFF = """\
You are a data-extraction assistant for the MGH Anesthesia Department. \
Extract after-5pm coverage staff from a pasted call schedule.

The format alternates: a SHIFT-TYPE LABEL on one line, then one or more NAMES on the \
following lines (one name per line), until the next label appears. \
Each label applies to every name listed beneath it until the next label.

Shift-type label → normalized shiftType mapping (exact MGH labels used):
  "1-A"                    → shiftType: "1-A",        role: "attending"
  "2-A"                    → shiftType: "2-A",        role: "attending"
  "7a7p-A"                 → shiftType: "7a-7p",      role: "attending"
  "3p10p-A"                → shiftType: "3p-10p",     role: "attending"
  "CRNA PM Inc5p - 8p"     → shiftType: "CRNA-5p-8p", role: "CRNA"
  "CRNA7a - 8p"            → shiftType: "CRNA-7a-8p", role: "CRNA"
  "R2"                     → role: "resident", residentLevel: "R2", shiftType: "3p-10p"
  "R3"                     → role: "resident", residentLevel: "R3", shiftType: "3p-10p"
  "R4"                     → role: "resident", residentLevel: "R4", shiftType: "3p-10p"

Also accept common variations: \
"1A"/"first call" → 1-A; "2A"/"second call" → 2-A; \
"7a-7p"/"7a7p" → 7a-7p; "3p-10p"/"3p10p" → 3p-10p; \
"CRNA.*5p" → CRNA-5p-8p; "CRNA.*7a" → CRNA-7a-8p; \
"CA-1"/"CA1" → R2; "CA-2"/"CA2" → R3; "CA-3"/"CA3" → R4.

Names are typically last name only or "LastFirst" run together — keep exactly as shown. \
alreadyDeployed = false unless the name is followed by an OR number, "in room", or "deployed". \
daytimeOR = null unless an OR number is mentioned alongside the name."""

_REFINE_TOOL: dict[str, Any] = {
    "name": "refine_plan",
    "description": (
        "Interpret natural language feedback and produce a list of specific edit operations "
        "to apply to the current anesthesia coverage plan. For each change the user requests, "
        "produce one edit operation. If a change violates a hard rule, mark it rejected and explain why."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": [
                                "set_attending",
                                "set_physical",
                                "remove_attending",
                                "remove_physical",
                                "swap_attendings",
                            ],
                            "description": (
                                "set_attending: assign a named attending to an OR (replaces current). "
                                "set_physical: assign a named CRNA or resident as physical provider. "
                                "remove_attending: remove the attending from an OR (leaves it unassigned). "
                                "remove_physical: remove physical provider (attending covers solo). "
                                "swap_attendings: swap the attending assignments between two ORs."
                            ),
                        },
                        "or_id": {
                            "type": "integer",
                            "description": "Primary OR number.",
                        },
                        "or_id_2": {
                            "type": ["integer", "null"],
                            "description": "Second OR number — only for swap_attendings.",
                        },
                        "name": {
                            "type": ["string", "null"],
                            "description": (
                                "Exact name from the staff list for set_attending / set_physical. "
                                "Use null for remove_* and swap_attendings."
                            ),
                        },
                        "rejected": {
                            "type": "boolean",
                            "description": "True when this change violates a hard rule and must not be applied.",
                        },
                        "rejection_reason": {
                            "type": ["string", "null"],
                            "description": "Human-readable explanation of why the change was rejected.",
                        },
                    },
                    "required": ["operation", "or_id", "rejected"],
                },
            },
            "summary": {
                "type": "string",
                "description": (
                    "Conversational response to the user: confirm what was understood, "
                    "list what will be changed, and explain anything that cannot be done."
                ),
            },
        },
        "required": ["edits", "summary"],
    },
}


async def parse_refinement(
    feedback: str,
    plan: dict[str, Any],
    rooms: list[dict[str, Any]],
    staff: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Interpret natural-language feedback and return structured edit operations.
    Returns {"edits": [...], "summary": str}.
    Raises ValueError if no API key; RuntimeError on unexpected LLM response.
    """
    # Summarise current assignments as readable text for the LLM context
    plan_lines = ["Current assignments:"]
    for a in sorted(plan.get("assignments", []), key=lambda x: x["or_id"]):
        phys = a.get("physical_provider")
        phys_type = a.get("physical_provider_type", "")
        phys_str = f"  physical={phys} ({phys_type})" if phys else "  solo attending"
        plan_lines.append(
            f"  OR {a['or_id']}: attending={a['attending']} ({a.get('attending_role','')}) {phys_str}"
        )
    for or_id in sorted(plan.get("unassigned_rooms", [])):
        plan_lines.append(f"  OR {or_id}: UNASSIGNED")

    staff_lines = ["Available staff (use exact names):"]
    for s in staff:
        restr = f"  [restrictions: {', '.join(s.get('restrictions', []))}]" if s.get("restrictions") else ""
        staff_lines.append(
            f"  {s['name']} | role={s['role']} | shift={s.get('shiftType','')}{restr}"
        )

    room_lines = ["OR details:"]
    for r in rooms:
        flags = []
        if r.get("flaggedFluoro"):  flags.append("fluoro")
        if r.get("flaggedComplex"): flags.append("complex")
        if r.get("isNewStart"):     flags.append("new-start")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        room_lines.append(f"  OR {r['id']}: {r.get('building','')} / {r.get('floor','')}{flag_str}")

    system = "\n".join([
        "You are a scheduling assistant for the MGH Anesthesia 5PM Coverage Planner.",
        "The user wants to adjust the current after-5pm assignment plan.",
        "Interpret their feedback and produce structured edit operations.",
        "",
        "Hard rules (enforce these — mark violating edits as rejected):",
        "  • An attending cannot supervise rooms in both Legacy AND Lunder buildings.",
        "  • Staff with 'no-fluoro' restriction cannot go into a fluoro-flagged OR.",
        "  • R2 residents may only go in complex-flagged ORs.",
        "  • A CRNA or resident must have an attending assigned to their OR.",
        "  • Use exact names from the staff list — do not invent names.",
        "",
        "\n".join(plan_lines),
        "",
        "\n".join(staff_lines),
        "",
        "\n".join(room_lines),
    ])

    client = _client()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=system,
        messages=[{
            "role": "user",
            "content": f"Please make the following changes to the plan:\n\n{feedback}",
        }],
        tools=[_REFINE_TOOL],
        tool_choice={"type": "tool", "name": "refine_plan"},
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "refine_plan":
            return {
                "edits":   block.input.get("edits", []),
                "summary": block.input.get("summary", ""),
            }

    raise RuntimeError("LLM did not return the expected tool call")
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is not set")
    return anthropic.AsyncAnthropic(api_key=key)


async def parse_or_schedule(text: str) -> dict[str, Any]:
    """
    Parse a pasted OR schedule with Claude.
    Returns {"rooms": {str(id): {attending, crna, resident}}, "count": int, "warnings": []}.
    Raises ValueError if no API key; raises RuntimeError on unexpected LLM response.
    """
    client = _client()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=_SYSTEM_SCHEDULE,
        messages=[{
            "role": "user",
            "content": f"Extract the OR room assignments from this schedule:\n\n{text}",
        }],
        tools=[_SCHEDULE_TOOL],
        tool_choice={"type": "tool", "name": "extract_schedule"},
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "extract_schedule":
            rooms: dict[str, Any] = {}
            for room in block.input.get("rooms", []):
                or_id = room.get("id")
                if or_id and isinstance(or_id, int) and 1 <= or_id <= 99:
                    rooms[str(or_id)] = {
                        "attending": room.get("daytimeAttending") or None,
                        "crna":      room.get("daytimeCRNA")      or None,
                        "resident":  room.get("daytimeResident")  or None,
                    }
            return {"rooms": rooms, "count": len(rooms), "warnings": []}

    raise RuntimeError("LLM did not return the expected tool call")


async def parse_staff_list(text: str) -> dict[str, Any]:
    """
    Parse a pasted after-5pm staff list with Claude.
    Returns {"staff": [...], "warnings": []}.
    Raises ValueError if no API key; raises RuntimeError on unexpected LLM response.
    """
    client = _client()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=_SYSTEM_STAFF,
        messages=[{
            "role": "user",
            "content": f"Extract the after-5pm staff from this list:\n\n{text}",
        }],
        tools=[_STAFF_TOOL],
        tool_choice={"type": "tool", "name": "extract_staff"},
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "extract_staff":
            staff = []
            for s in block.input.get("staff", []):
                if not s.get("name"):
                    continue
                staff.append({
                    "name":            s["name"],
                    "role":            s.get("role", "attending"),
                    "shiftType":       s.get("shiftType", "1-A"),
                    "residentLevel":   s.get("residentLevel"),
                    "availablePast5pm": True,
                    "restrictions":    s.get("restrictions") or [],
                    "affinities":      [],
                    "daytimeOR":       s.get("daytimeOR"),
                    "alreadyDeployed": bool(s.get("alreadyDeployed", False)),
                })
            return {"staff": staff, "warnings": []}

    raise RuntimeError("LLM did not return the expected tool call")
