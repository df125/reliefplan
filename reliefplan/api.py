"""FastAPI web application for the MGH Anesthesia 5PM Coverage Planner."""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .algorithm import plan as run_plan
from .loader import _parse_room, _parse_staff

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

    result = run_plan(rooms, staff)
    return {
        **dataclasses.asdict(result),
        # Lookup maps for the frontend (keyed by string for JSON compatibility)
        "room_map": {str(r.id): dataclasses.asdict(r) for r in rooms},
        "staff_map": {s.name: dataclasses.asdict(s) for s in staff},
    }


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
