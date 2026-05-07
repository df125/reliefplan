"""FastAPI web application for the MGH Anesthesia 5PM Coverage Planner."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .affinities import load_affinities, merge_affinities, save_affinities
from .algorithm import plan as run_plan
from .loader import _parse_room, _parse_staff, VALID_SHIFT_TYPES
from .parser import parse_or_schedule, parse_situation, parse_staff_list, parse_refinement
from .refine import apply_edits
from .roster import load_roster, merge_roster, save_roster

app = FastAPI(title="MGH Anesthesia Coverage Planner", docs_url=None, redoc_url=None)

STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# API routes (must be registered BEFORE the static file mount)
# ---------------------------------------------------------------------------

@app.post("/api/plan")
async def api_plan(request: Request) -> dict[str, Any]:
    body = await request.json()
    try:
        rooms = [_parse_room(r, i) for i, r in enumerate(body.get("rooms", []))]
        staff = [_parse_staff(s, i) for i, s in enumerate(body.get("staff", []))]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if not rooms:
        raise HTTPException(status_code=422, detail="No rooms provided")
    if not staff:
        raise HTTPException(status_code=422, detail="No staff provided")

    # Merge persisted affinities into each staff member before planning
    stored = load_affinities()
    for s in staff:
        extra = stored.get(s.name, [])
        for tag in extra:
            if tag not in s.affinities:
                s.affinities.append(tag)

    result = run_plan(rooms, staff)

    # Silently accumulate staff into the persistent roster
    try:
        existing_roster = load_roster()
        updated_roster  = merge_roster(existing_roster, body.get("staff", []))
        save_roster(updated_roster)
    except Exception:
        pass

    return {
        **dataclasses.asdict(result),
        # Lookup maps for the frontend (keyed by string for JSON compatibility)
        "room_map": {str(r.id): dataclasses.asdict(r) for r in rooms},
        "staff_map": {s.name: dataclasses.asdict(s) for s in staff},
    }


@app.post("/api/refine")
async def api_refine(request: Request) -> dict[str, Any]:
    body = await request.json()
    feedback = (body.get("feedback") or "").strip()
    if not feedback:
        raise HTTPException(status_code=422, detail="feedback is required")

    rooms_raw = body.get("rooms", [])
    staff_raw = body.get("staff", [])
    current_plan = body.get("plan", {})

    try:
        rooms = [_parse_room(r, i) for i, r in enumerate(rooms_raw)]
        staff = [_parse_staff(s, i) for i, s in enumerate(staff_raw)]
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    try:
        refine_result = await parse_refinement(feedback, current_plan, rooms_raw, staff_raw)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Refinement error: {exc}")

    # Promote stay-late daytime providers into the PM staff list
    added_staff_names: list[str] = []
    for sa in refine_result.get("staff_additions", []):
        sa_name = (sa.get("name") or "").strip()
        if not sa_name or any(s.name == sa_name for s in staff):
            continue
        daytime_or = sa.get("daytime_or") or next(
            (r.id for r in rooms if r.daytime_attending == sa_name), None
        )
        is_moon = bool(sa.get("isMoonlighter"))
        shift = sa.get("shiftType") or ("moonlighter" if is_moon else "7a-7p")
        if shift not in VALID_SHIFT_TYPES:
            shift = "moonlighter" if is_moon else "7a-7p"
        staff_dict = {
            "name": sa_name, "role": sa.get("role") or "attending",
            "shiftType": shift, "availablePast5pm": True,
            "restrictions": [], "affinities": [],
            "daytimeOR": daytime_or, "alreadyDeployed": not is_moon,
            "isMoonlighter": is_moon, "departureTarget": "",
        }
        try:
            staff.append(_parse_staff(staff_dict, len(staff)))
            staff_raw.append(staff_dict)
            added_staff_names.append(sa_name)
        except ValueError:
            pass

    updated_plan, applied, rejected = apply_edits(
        current_plan, refine_result["edits"], rooms, staff
    )

    # Persist any affinity updates extracted by the LLM
    affinity_updates = refine_result.get("affinity_updates", [])
    affinities_saved = False
    if affinity_updates:
        existing = load_affinities()
        merged = merge_affinities(existing, affinity_updates)
        save_affinities(merged)
        affinities_saved = True

    return {
        **updated_plan,
        "room_map":           {str(r.id): dataclasses.asdict(r) for r in rooms},
        "staff_map":          {s.name: dataclasses.asdict(s) for s in staff},
        "refine_summary":     refine_result["summary"],
        "changes_applied":    applied,
        "changes_rejected":   rejected,
        "affinities_saved":   affinities_saved,
        "staff_added":        added_staff_names,
        "or_list_changes":    refine_result.get("or_list_changes", []),
        "unhandled_requests": refine_result.get("unhandled_requests", []),
    }


@app.post("/api/situation")
async def api_situation(request: Request) -> dict[str, Any]:
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    rooms_raw = body.get("rooms", [])
    staff_raw = body.get("staff", [])
    try:
        result = await parse_situation(text, rooms_raw, staff_raw)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Situation parse error: {exc}")
    return result


@app.post("/api/parse/schedule")
async def api_parse_schedule(request: Request) -> dict[str, Any]:
    body = await request.json()
    text = body.get("text", "")
    if not text.strip():
        raise HTTPException(status_code=422, detail="text is required")
    try:
        return await parse_or_schedule(text)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Parse error: {exc}")


@app.post("/api/parse/staff")
async def api_parse_staff(request: Request) -> dict[str, Any]:
    body = await request.json()
    text = body.get("text", "")
    if not text.strip():
        raise HTTPException(status_code=422, detail="text is required")
    try:
        return await parse_staff_list(text)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Parse error: {exc}")


@app.get("/api/sample")
def api_sample() -> dict[str, Any]:
    # Import here to avoid circular dependency at module load time
    from .cli import SAMPLE_INPUT
    return SAMPLE_INPUT


# ---------------------------------------------------------------------------
# Serve the single-page frontend
# ---------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
else:
    @app.get("/")
    def root() -> HTMLResponse:
        return HTMLResponse("<h1>Static directory not found. Run from the project root.</h1>")
