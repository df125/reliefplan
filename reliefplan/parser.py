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
Anesthesia Department. Extract OR room assignments from pasted schedule text. \
Be liberal in recognising column header synonyms: \
"Room"/"Suite"/"OR#" → OR number; \
"Attending"/"Anes MD"/"Anesthesiologist" → attending; \
"CRNA"/"AA"/"Nurse Anesthetist" → CRNA; \
"Resident"/"CA"/"Fellow"/"Intern" → resident. \
Keep names exactly as they appear; do not reformat them."""

_SYSTEM_STAFF = """\
You are a data-extraction assistant for the MGH Anesthesia Department. \
Extract after-5pm coverage staff from a pasted call schedule or staff list. \
The text may have section headers such as "Attendings:", "CRNAs:", "Residents:". \
Infer role from context when not explicit: \
a person listed under "CRNAs" is a CRNA; \
"R2"/"R3"/"R4"/"CA-1"/"CA-2"/"CA-3" indicates a resident. \
Infer shift type from keywords: \
"1A"/"1-A"/"first call" → 1-A; \
"2A"/"2-A"/"second call" → 2-A; \
"3p"/"3pm"/"3-10" → 3p-10p; \
"stay"/"staying"/"7a-7p" → 7a-7p; \
"5p CRNA"/"eve CRNA"/"5-8" → CRNA-5p-8p; \
"7a CRNA"/"day CRNA" → CRNA-7a-8p. \
alreadyDeployed = true when: the name is followed by an OR number, \
"in room", "deployed", or similar language."""


def _client() -> anthropic.AsyncAnthropic:
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
