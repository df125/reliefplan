"""
LLM-based text parsing for OR schedule and after-5pm staff lists.

Uses Google Gemini with function calling to reliably extract structured data
from free-form pasted text regardless of format or column order.
Falls back gracefully if the API key is absent.
"""
from __future__ import annotations

import json
import os
from typing import Any

from google import genai
from google.genai import types as gtypes

_MODEL = "gemini-2.5-flash"

# ── Tool schemas (OpenAPI format — converted to Gemini at call time) ───────────

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
                            "type": "string",
                            "description": (
                                "OR number (1–99) as a string for standard ORs. "
                                "For EP/IR/Endo named columns use the column name as-is (e.g. 'EP1', 'ENDO6', 'ADULT RAD A')."
                            ),
                        },
                        "locationType": {
                            "type": "string",
                            "enum": ["OR", "IR", "Endo", "EP"],
                            "description": (
                                "OR for numbered operating rooms. "
                                "EP for EP1-EP5/EP Mid columns in Cardiac section. "
                                "IR for ADULT RAD A-D columns in OMOR section. "
                                "Endo for ENDO6-ENDO11 columns in ENDO section."
                            ),
                        },
                        "daytimeAttending": {
                            "type": "string",
                            "description": "Attending anesthesiologist name. Empty string if not listed.",
                        },
                        "daytimeCRNA": {
                            "type": "string",
                            "description": "CRNA or AA name. Empty string if not listed.",
                        },
                        "daytimeResident": {
                            "type": "string",
                            "description": "Resident or fellow name. Empty string if not listed.",
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
        "Extract after-5pm anesthesia staff from a pasted call schedule or staff list."
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
                            "type": "string",
                            "enum": ["R2", "R3", "R4", ""],
                            "description": "Only for residents. CA-1=R2, CA-2=R3, CA-3=R4. Empty string otherwise.",
                        },
                        "daytimeOR": {
                            "type": "integer",
                            "description": "OR number covered during the day, or 0 if not mentioned.",
                        },
                        "alreadyDeployed": {
                            "type": "boolean",
                            "description": (
                                "True if already placed in a room before 5pm "
                                "(indicated by 'in room', 'deployed', 'OR ##', etc.)."
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

_SITUATION_TOOL: dict[str, Any] = {
    "name": "parse_situation",
    "description": (
        "Parse a free-form situation-update message from the OR coordinator and extract "
        "structured changes: OR flag updates, room closures, team moves, staff role swaps, "
        "and new staff additions (moonlighters, stay-late attendings)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "room_flag_updates": {
                "type": "array",
                "description": "Changes to OR flags or estimated end time.",
                "items": {
                    "type": "object",
                    "properties": {
                        "or_id":         {"type": "integer"},
                        "flaggedComplex":{"type": "boolean"},
                        "flaggedFluoro": {"type": "boolean"},
                        "isNewStart":    {"type": "boolean"},
                        "estimatedEnd":  {"type": "string", "description": "e.g. '21:00' or '10pm'"},
                    },
                    "required": ["or_id"],
                },
            },
            "room_closures": {
                "type": "array",
                "description": "ORs that closed or were cancelled — remove from the late list.",
                "items": {
                    "type": "object",
                    "properties": {"or_id": {"type": "integer"}},
                    "required": ["or_id"],
                },
            },
            "team_moves": {
                "type": "array",
                "description": "A room's team was physically moved to a different OR number.",
                "items": {
                    "type": "object",
                    "properties": {
                        "from_or": {"type": "integer"},
                        "to_or":   {"type": "integer"},
                    },
                    "required": ["from_or", "to_or"],
                },
            },
            "or_staff_swaps": {
                "type": "array",
                "description": "Change a specific role in a specific OR card.",
                "items": {
                    "type": "object",
                    "properties": {
                        "or_id": {"type": "integer"},
                        "role":  {"type": "string", "enum": ["attending", "crna", "resident"]},
                        "name":  {"type": "string"},
                    },
                    "required": ["or_id", "role", "name"],
                },
            },
            "staff_additions": {
                "type": "array",
                "description": (
                    "Use for two cases: (1) new PM staff not previously on the list "
                    "(moonlighters, stay-late daytime attendings); (2) an existing PM staff member "
                    "whose daytimeOR or alreadyDeployed status needs to be updated — e.g. 'White-Dzuro "
                    "has been in OR 90 since 3pm'. In case 2, include their existing name exactly and "
                    "the system will update their row rather than add a duplicate."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name":            {"type": "string"},
                        "role":            {"type": "string", "enum": ["attending", "CRNA", "resident"]},
                        "shiftType":       {
                            "type": "string",
                            "enum": ["1-A", "2-A", "7a-7p", "3p-10p", "CRNA-7a-8p", "CRNA-5p-8p", "moonlighter"],
                        },
                        "isMoonlighter":   {"type": "boolean"},
                        "daytimeOR":       {
                            "type": "integer",
                            "description": "OR number the provider is currently in or covered during the day.",
                        },
                        "departureTarget": {"type": "string", "description": "e.g. '22:00'"},
                        "alreadyDeployed": {"type": "boolean"},
                    },
                    "required": ["name", "role"],
                },
            },
            "summary": {
                "type": "string",
                "description": "Brief natural-language confirmation of what was understood.",
            },
        },
        "required": ["room_flag_updates", "room_closures", "team_moves", "or_staff_swaps", "staff_additions", "summary"],
    },
}

_REFINE_TOOL: dict[str, Any] = {
    "name": "refine_plan",
    "description": (
        "Interpret natural language feedback and produce a list of specific edit operations "
        "to apply to the current anesthesia coverage plan. For each change the user requests, "
        "produce one edit operation. If a change violates a hard rule, mark it rejected."
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
                                "set_attending: assign a named attending to an OR. "
                                "set_physical: assign a named CRNA or resident as physical provider. "
                                "remove_attending: remove the attending (leaves OR unassigned). "
                                "remove_physical: remove physical provider (attending covers solo). "
                                "swap_attendings: swap attending assignments between two ORs."
                            ),
                        },
                        "or_id": {
                            "type": "integer",
                            "description": "Primary OR number.",
                        },
                        "or_id_2": {
                            "type": "integer",
                            "description": "Second OR number — only for swap_attendings. Use 0 if not applicable.",
                        },
                        "name": {
                            "type": "string",
                            "description": (
                                "Exact name from the staff list for set_attending / set_physical. "
                                "Empty string for remove_* and swap_attendings."
                            ),
                        },
                        "rejected": {
                            "type": "boolean",
                            "description": "True when this change violates a hard rule.",
                        },
                        "rejection_reason": {
                            "type": "string",
                            "description": "Why the change was rejected. Empty string if not rejected.",
                        },
                    },
                    "required": ["operation", "or_id", "rejected"],
                },
            },
            "summary": {
                "type": "string",
                "description": (
                    "Conversational response: confirm what was understood, "
                    "list what will be changed, and explain anything that cannot be done."
                ),
            },
            "affinity_updates": {
                "type": "array",
                "description": (
                    "If the user mentions a preference (e.g. 'Dr. Smith tends to work L4'), "
                    "record it here so it can be persisted."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name":   {"type": "string"},
                        "add":    {"type": "array", "items": {"type": "string"}},
                        "remove": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["name", "add", "remove"],
                },
            },
            "staff_additions": {
                "type": "array",
                "description": (
                    "Populate for two cases: (1) a daytime provider agreed to stay late — "
                    "use their exact full name from the daytime providers list; "
                    "(2) a moonlighting attending is joining for the evening — "
                    "use the name as given, set isMoonlighter=true, shiftType='moonlighter'. "
                    "Leave empty if neither case applies."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name":          {"type": "string"},
                        "role":          {"type": "string", "enum": ["attending", "CRNA", "resident"]},
                        "shiftType":     {
                            "type": "string",
                            "enum": ["1-A", "2-A", "7a-7p", "3p-10p", "CRNA-7a-8p", "CRNA-5p-8p", "moonlighter"],
                            "description": "Use 'moonlighter' for moonlighting attendings; '7a-7p' for a daytime attending staying late.",
                        },
                        "isMoonlighter": {
                            "type": "boolean",
                            "description": "Set true when the person is described as moonlighting or moonlighter.",
                        },
                        "daytime_or": {
                            "type": "integer",
                            "description": "OR number the provider covered during the day (omit for moonlighters).",
                        },
                    },
                    "required": ["name", "role", "shiftType"],
                },
            },
            "or_list_changes": {
                "type": "array",
                "description": (
                    "Use when the user adds or removes an OR from the late-running list. "
                    "For remove_or, also emit a remove_attending edit in edits[] to keep the plan consistent."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["add_or", "remove_or"]},
                        "or_id":     {"type": "integer"},
                    },
                    "required": ["operation", "or_id"],
                },
            },
            "unhandled_requests": {
                "type": "array",
                "description": (
                    "Populate when the user asks for something you cannot express as any current operation "
                    "(e.g. queries, analytics, bulk swaps with no clear target). "
                    "Describe what was requested and why it is not currently supported."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "original_text":     {"type": "string",
                                              "description": "Verbatim excerpt from the user request."},
                        "reason":            {"type": "string",
                                              "description": "Why this cannot be handled currently."},
                        "suggested_feature": {"type": "string",
                                              "description": "One-sentence feature description for a developer."},
                    },
                    "required": ["original_text", "reason", "suggested_feature"],
                },
            },
        },
        "required": ["edits", "summary", "affinity_updates", "staff_additions", "or_list_changes", "unhandled_requests"],
    },
}

# ── System prompts ─────────────────────────────────────────────────────────────

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
- "Tutor, Tutee" is a teaching-pair PLACEHOLDER, NOT a real person — treat as empty string.
- Names appear as "Last, First M" or "Last, First Middle" — keep exactly as shown.
- OR numbers range 1–99; ignore any text that is not a 1–2 digit integer in the OR-number row.
- Within the "Cardiac" section, columns with headers matching "EP1"–"EP5" or "EP Mid" are EP labs. \
Set locationType="EP" for these and use the column name as the id. Do not create entries for Cath1/Cath2/Echo.
- OR 48 in the Cardiac section is a standard gen surg OR; set locationType="OR", id="48". \
Ignore ORs 45, 46, 47, 49 (cardiac-specific, not relevant for evening coverage).
- The "ENDO" section contains Endo rooms (ENDO6–ENDO11). Set locationType="Endo" and use the column name as the id.
- Within the "OMOR" section, columns matching "ADULT RAD A/B/C/D" are IR rooms. \
Set locationType="IR" and use the column name as the id. Ignore all other OMOR columns (OB, PACU, ECT, etc.).
- For EP/IR/Endo rooms: still extract attending and CRNA names — these are used for daytime provenance only.

Return one entry per OR or named offsite room found."""

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

Names are typically last name only — keep exactly as shown. \
alreadyDeployed = false unless the name is followed by an OR number, "in room", or "deployed". \
daytimeOR = 0 unless an OR number is mentioned alongside the name."""


# ── Gemini helpers ─────────────────────────────────────────────────────────────

def _client() -> genai.Client:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise ValueError("GEMINI_API_KEY environment variable is not set")
    return genai.Client(api_key=key)


def _to_schema(s: dict[str, Any]) -> gtypes.Schema:
    """Recursively convert an OpenAPI-style schema dict to a Gemini Schema."""
    type_map = {
        "object":  "OBJECT",
        "array":   "ARRAY",
        "string":  "STRING",
        "integer": "INTEGER",
        "boolean": "BOOLEAN",
        "number":  "NUMBER",
    }
    raw_type = s.get("type", "string")
    if isinstance(raw_type, list):
        raw_type = next((t for t in raw_type if t != "null"), "string")

    kwargs: dict[str, Any] = {"type": type_map.get(raw_type, "STRING")}

    if "description" in s:
        kwargs["description"] = s["description"]
    if raw_type == "object" and "properties" in s:
        kwargs["properties"] = {k: _to_schema(v) for k, v in s["properties"].items()}
    if raw_type == "object" and "required" in s:
        kwargs["required"] = s["required"]
    if raw_type == "array" and "items" in s:
        kwargs["items"] = _to_schema(s["items"])
    if "enum" in s:
        vals = [str(e) for e in s["enum"] if e is not None and e != ""]
        if vals:
            kwargs["enum"] = vals

    return gtypes.Schema(**kwargs)


def _make_tool(tool_def: dict[str, Any]) -> gtypes.Tool:
    """Convert an Anthropic-format tool dict to a Gemini Tool."""
    return gtypes.Tool(function_declarations=[
        gtypes.FunctionDeclaration(
            name=tool_def["name"],
            description=tool_def["description"],
            parameters=_to_schema(tool_def["input_schema"]),
        )
    ])


def _forced_config(system: str, tool_name: str, tool_def: dict[str, Any]) -> gtypes.GenerateContentConfig:
    return gtypes.GenerateContentConfig(
        system_instruction=system,
        tools=[_make_tool(tool_def)],
        tool_config=gtypes.ToolConfig(
            function_calling_config=gtypes.FunctionCallingConfig(
                mode="ANY",
                allowed_function_names=[tool_name],
            )
        ),
    )


def _extract_fc_args(response: Any, tool_name: str) -> dict[str, Any] | None:
    """Pull function-call args out of a Gemini response as a plain Python dict."""
    for candidate in (response.candidates or []):
        for part in (candidate.content.parts or []):
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", None) == tool_name:
                args = fc.args or {}
                # Round-trip through JSON to guarantee plain Python types
                try:
                    return json.loads(json.dumps(dict(args)))
                except (TypeError, ValueError):
                    return dict(args)
    return None


# ── Public async functions ─────────────────────────────────────────────────────

async def parse_or_schedule(text: str) -> dict[str, Any]:
    """
    Parse a pasted OR schedule with Gemini.
    Returns {"rooms": {str(id): {attending, crna, resident}}, "count": int, "warnings": []}.
    Raises ValueError if no API key; RuntimeError on unexpected response.
    """
    client = _client()
    response = await client.aio.models.generate_content(
        model=_MODEL,
        contents=f"Extract the OR room assignments from this schedule:\n\n{text}",
        config=_forced_config(_SYSTEM_SCHEDULE, "extract_schedule", _SCHEDULE_TOOL),
    )

    args = _extract_fc_args(response, "extract_schedule")
    if args is None:
        raise RuntimeError("LLM did not return the expected function call")

    rooms: dict[str, Any] = {}
    offsite_count = 0
    for room in args.get("rooms", []):
        or_id_raw = room.get("id")
        loc_type = room.get("locationType", "OR")
        room_entry = {
            "attending":    room.get("daytimeAttending") or None,
            "crna":         room.get("daytimeCRNA")      or None,
            "resident":     room.get("daytimeResident")  or None,
            "locationType": loc_type,
        }
        if loc_type != "OR":
            # Named offsite room — store with string key for provenance only
            name_key = str(or_id_raw) if or_id_raw else None
            if name_key:
                rooms[name_key] = room_entry
                offsite_count += 1
        else:
            try:
                or_id = int(or_id_raw)
            except (TypeError, ValueError):
                continue
            if 1 <= or_id <= 99:
                rooms[str(or_id)] = room_entry
    or_count = len(rooms) - offsite_count
    return {"rooms": rooms, "count": or_count, "warnings": []}


async def parse_staff_list(text: str) -> dict[str, Any]:
    """
    Parse a pasted after-5pm staff list with Gemini.
    Returns {"staff": [...], "warnings": []}.
    Raises ValueError if no API key; RuntimeError on unexpected response.
    """
    client = _client()
    response = await client.aio.models.generate_content(
        model=_MODEL,
        contents=f"Extract the after-5pm staff from this list:\n\n{text}",
        config=_forced_config(_SYSTEM_STAFF, "extract_staff", _STAFF_TOOL),
    )

    args = _extract_fc_args(response, "extract_staff")
    if args is None:
        raise RuntimeError("LLM did not return the expected function call")

    staff = []
    for s in args.get("staff", []):
        if not s.get("name"):
            continue
        daytime_or = s.get("daytimeOR") or 0
        try:
            daytime_or = int(daytime_or) or None
        except (TypeError, ValueError):
            daytime_or = None
        res_level = s.get("residentLevel") or None
        if res_level == "":
            res_level = None
        staff.append({
            "name":             s["name"],
            "role":             s.get("role", "attending"),
            "shiftType":        s.get("shiftType", "1-A"),
            "residentLevel":    res_level,
            "availablePast5pm": True,
            "restrictions":     s.get("restrictions") or [],
            "affinities":       [],
            "daytimeOR":        daytime_or,
            "alreadyDeployed":  bool(s.get("alreadyDeployed", False)),
        })
    return {"staff": staff, "warnings": []}


async def parse_refinement(
    feedback: str,
    plan: dict[str, Any],
    rooms: list[dict[str, Any]],
    staff: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Interpret natural-language feedback and return structured edit operations.
    Returns {"edits": [...], "summary": str}.
    Raises ValueError if no API key; RuntimeError on unexpected response.
    """
    plan_lines = ["Current assignments:"]
    for a in sorted(plan.get("assignments", []), key=lambda x: x["or_id"]):
        phys = a.get("physical_provider")
        phys_type = a.get("physical_provider_type", "")
        phys_str = f"physical={phys} ({phys_type})" if phys else "solo attending"
        plan_lines.append(
            f"  OR {a['or_id']}: attending={a['attending']} ({a.get('attending_role','')}) | {phys_str}"
        )
    for or_id in sorted(plan.get("unassigned_rooms", [])):
        plan_lines.append(f"  OR {or_id}: UNASSIGNED")

    staff_lines = ["Available staff (use exact names):"]
    for s in staff:
        restr = f" [restrictions: {', '.join(s.get('restrictions', []))}]" if s.get("restrictions") else ""
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

    daytime_lines = ["Daytime providers by OR (not on PM staff — candidates if user says they agreed to stay):"]
    for r in rooms:
        dt_att  = r.get("daytimeAttending")  or r.get("daytime_attending")  or ""
        dt_crna = r.get("daytimeCRNA")        or r.get("daytime_crna")        or ""
        dt_res  = r.get("daytimeResident")   or r.get("daytime_resident")   or ""
        parts = (
            ([f"attending={dt_att}"]  if dt_att  else []) +
            ([f"CRNA={dt_crna}"]      if dt_crna else []) +
            ([f"resident={dt_res}"]   if dt_res  else [])
        )
        if parts:
            daytime_lines.append(f"  OR {r.get('id','?')}: {', '.join(parts)}")
    if len(daytime_lines) == 1:
        daytime_lines.append("  (none)")

    system = "\n".join([
        "You are a scheduling assistant for the MGH Anesthesia 5PM Coverage Planner.",
        "The user wants to adjust the current after-5pm assignment plan.",
        "Interpret their feedback and produce structured edit operations.",
        "",
        "Hard rules (enforce — mark violating edits as rejected=true):",
        "  • An attending cannot supervise rooms in both Legacy AND Lunder buildings.",
        "  • Staff with 'no-fluoro' restriction cannot go into a fluoro-flagged OR.",
        "  • A CRNA or resident must have an attending assigned to their OR.",
        "  • Use exact names from the PM staff list or daytime providers list — do not invent names.",
        "  • If the user gives only a last name, match it against PM staff or daytime providers below.",
        "    Use the full name when a unique match is found.",
        "  • If a daytime provider agreed to stay, add them to staff_additions with their exact",
        "    full name; do NOT mark that assignment edit as rejected.",
        "  • If the user mentions moonlighting attendings (e.g. 'We have moonlighters X and Y'),",
        "    add each to staff_additions with role='attending', shiftType='moonlighter',",
        "    isMoonlighter=true. Use the name exactly as given. Do not reject these.",
        "  • If the user says an OR is no longer running late, emit remove_or in or_list_changes",
        "    AND emit remove_attending for that OR in edits.",
        "  • If the user says a new OR is running late, emit add_or in or_list_changes.",
        "    Do not invent an attending assignment — the coordinator will fill in providers.",
        "  • If any part of the user's request cannot be expressed as a supported operation,",
        "    record it in unhandled_requests with a clear suggested_feature for a developer.",
        "    Do NOT silently drop unrecognised intent.",
        "",
        "\n".join(plan_lines),
        "",
        "\n".join(staff_lines),
        "",
        "\n".join(room_lines),
        "",
        "\n".join(daytime_lines),
    ])

    client = _client()
    response = await client.aio.models.generate_content(
        model=_MODEL,
        contents=f"Please make the following changes to the plan:\n\n{feedback}",
        config=_forced_config(system, "refine_plan", _REFINE_TOOL),
    )

    args = _extract_fc_args(response, "refine_plan")
    if args is None:
        raise RuntimeError("LLM did not return the expected function call")

    # Normalise edits: or_id_2=0 means absent, name="" means absent
    edits = []
    for e in args.get("edits", []):
        edits.append({
            **e,
            "or_id_2":          e.get("or_id_2") or None,
            "name":             e.get("name") or None,
            "rejection_reason": e.get("rejection_reason") or None,
        })

    return {
        "edits":              edits,
        "summary":            args.get("summary", ""),
        "affinity_updates":   args.get("affinity_updates", []),
        "staff_additions":    args.get("staff_additions", []),
        "or_list_changes":    args.get("or_list_changes", []),
        "unhandled_requests": args.get("unhandled_requests", []),
    }


_SYSTEM_SITUATION = """\
You are a scheduling assistant for the MGH Anesthesia 5PM Coverage Planner.
The coordinator has typed a free-form situation update describing changes that have occurred
since the initial plan was entered. Parse the message and extract structured changes.

Guidelines:
- room_closures: OR was cancelled or closed entirely; remove it from the late-running list.
- team_moves: the entire team from one OR physically moved to a different OR number (common when
  a case is bumped to another room). Copy daytime attending/crna/resident fields.
- or_staff_swaps: a specific role in an OR changed (e.g. "attending in OR 12 is now Dr. Jones").
- room_flag_updates: OR complexity/fluoro flags changed, or estimated end time was stated.
- staff_additions: covers two cases:
    (a) Truly new PM staff (moonlighters, stay-late daytime attendings not yet on the list).
        Set isMoonlighter=true if described as "moonlighter" or "moonlighting".
        Set departureTarget to a 24h time string if a specific departure time is mentioned.
        IMPORTANT: For a daytime attending who is staying late, look up their OR number from
        the room list above (which shows "daytime attending: Name" for each room). Set daytimeOR
        to that OR number and alreadyDeployed=true so the algorithm keeps them in their own room.
    (b) An EXISTING PM staff member whose deployment to an OR is being reported (e.g. "White-Dzuro
        has been in OR 90 since 3pm" when White-Dzuro is already on the PM list). In this case,
        include them in staff_additions with their exact current name, alreadyDeployed=true, and
        daytimeOR set to the OR number. Also emit an or_staff_swap for that OR to update the OR card.
        Do NOT invent a new row — the system will detect the existing name and update it in place.
- Return empty arrays for categories that have no changes.
- summary: one or two sentences confirming what you understood."""


async def parse_situation(
    text: str,
    rooms: list[dict[str, Any]],
    staff: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Parse a natural-language situation update and return structured diffs.
    Returns the full tool-call output dict.
    Raises ValueError if no API key; RuntimeError on unexpected response.
    """
    room_context = ", ".join(
        "OR {id} ({floor}, daytime attending: {att})".format(
            id=r.get("id"),
            floor=r.get("floor", "?"),
            att=r.get("daytimeAttending") or r.get("daytime_attending") or "unknown",
        )
        for r in rooms
    )
    staff_context = ", ".join(
        f"{s.get('name')} ({s.get('role')})" for s in staff
    )
    context = (
        f"Current late-running ORs: {room_context or 'none listed'}.\n"
        f"Current PM staff: {staff_context or 'none listed'}."
    )

    client = _client()
    response = await client.aio.models.generate_content(
        model=_MODEL,
        contents=f"{context}\n\nSituation update:\n{text}",
        config=_forced_config(_SYSTEM_SITUATION, "parse_situation", _SITUATION_TOOL),
    )

    args = _extract_fc_args(response, "parse_situation")
    if args is None:
        raise RuntimeError("LLM did not return the expected function call")

    return {
        "room_flag_updates": args.get("room_flag_updates", []),
        "room_closures":     args.get("room_closures", []),
        "team_moves":        args.get("team_moves", []),
        "or_staff_swaps":    args.get("or_staff_swaps", []),
        "staff_additions":   args.get("staff_additions", []),
        "summary":           args.get("summary", ""),
    }
