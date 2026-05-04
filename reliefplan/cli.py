"""CLI entry point for the MGH Anesthesia 5PM Coverage Planner."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from rich.console import Console

from .algorithm import plan
from .loader import load
from .output import console, render_plan

err_console = Console(stderr=True)

SAMPLE_INPUT = {
    "rooms": [
        {
            "id": 44,
            "building": "Legacy",
            "floor": "THOR",
            "daytimeAttending": "Dr. Patel",
            "daytimeCRNA": "Jones",
            "daytimeResident": None,
            "isLateRunning": True,
            "flaggedComplex": True,
            "flaggedFluoro": False,
            "estimatedEnd": "10pm",
            "isNewStart": False
        },
        {
            "id": 5,
            "building": "Legacy",
            "floor": "Gray",
            "daytimeAttending": "Dr. Patel",
            "daytimeCRNA": None,
            "daytimeResident": "Dr. Kim",
            "isLateRunning": True,
            "flaggedComplex": False,
            "flaggedFluoro": False,
            "estimatedEnd": "8pm",
            "isNewStart": False
        },
        {
            "id": 15,
            "building": "Legacy",
            "floor": "THOR",
            "daytimeAttending": "Dr. Rivera",
            "daytimeCRNA": "Santos",
            "daytimeResident": None,
            "isLateRunning": True,
            "flaggedComplex": False,
            "flaggedFluoro": True,
            "estimatedEnd": "9pm",
            "isNewStart": False
        },
        {
            "id": 63,
            "building": "Lunder",
            "floor": "L3",
            "daytimeAttending": "Dr. Chen",
            "daytimeCRNA": "Williams",
            "daytimeResident": None,
            "isLateRunning": True,
            "flaggedComplex": False,
            "flaggedFluoro": False,
            "estimatedEnd": "midnight",
            "isNewStart": False
        },
        {
            "id": 71,
            "building": "Lunder",
            "floor": "L3",
            "daytimeAttending": "Dr. Chen",
            "daytimeCRNA": None,
            "daytimeResident": None,
            "isLateRunning": True,
            "flaggedComplex": False,
            "flaggedFluoro": False,
            "estimatedEnd": None,
            "isNewStart": True
        },
        {
            "id": 82,
            "building": "Lunder",
            "floor": "L4",
            "daytimeAttending": "Dr. Lopez",
            "daytimeCRNA": "Park",
            "daytimeResident": None,
            "isLateRunning": True,
            "flaggedComplex": True,
            "flaggedFluoro": False,
            "estimatedEnd": "9pm",
            "isNewStart": False
        }
    ],
    "staff": [
        {
            "name": "Dr. Johnson",
            "role": "attending",
            "residentLevel": None,
            "shiftType": "1-A",
            "availablePast5pm": True,
            "restrictions": [],
            "affinities": ["Legacy"],
            "daytimeOR": None,
            "alreadyDeployed": False
        },
        {
            "name": "Dr. Williams",
            "role": "attending",
            "residentLevel": None,
            "shiftType": "3p-10p",
            "availablePast5pm": True,
            "restrictions": [],
            "affinities": ["Lunder", "L3"],
            "daytimeOR": 63,
            "alreadyDeployed": True
        },
        {
            "name": "Dr. Brown",
            "role": "attending",
            "residentLevel": None,
            "shiftType": "2-A",
            "availablePast5pm": True,
            "restrictions": [],
            "affinities": ["Lunder"],
            "daytimeOR": 82,
            "alreadyDeployed": True
        },
        {
            "name": "Dr. Torres",
            "role": "attending",
            "residentLevel": None,
            "shiftType": "7a-7p",
            "availablePast5pm": True,
            "restrictions": ["no-fluoro"],
            "affinities": ["Legacy", "THOR"],
            "daytimeOR": 44,
            "alreadyDeployed": False
        },
        {
            "name": "Chen",
            "role": "CRNA",
            "residentLevel": None,
            "shiftType": "CRNA-5p-8p",
            "availablePast5pm": True,
            "restrictions": [],
            "affinities": ["Lunder"],
            "daytimeOR": None,
            "alreadyDeployed": False
        },
        {
            "name": "Jones",
            "role": "CRNA",
            "residentLevel": None,
            "shiftType": "CRNA-7a-8p",
            "availablePast5pm": True,
            "restrictions": [],
            "affinities": ["THOR"],
            "daytimeOR": 44,
            "alreadyDeployed": False
        },
        {
            "name": "Santos",
            "role": "CRNA",
            "residentLevel": None,
            "shiftType": "CRNA-7a-8p",
            "availablePast5pm": True,
            "restrictions": ["no-fluoro"],
            "affinities": ["THOR"],
            "daytimeOR": 15,
            "alreadyDeployed": False
        },
        {
            "name": "Williams",
            "role": "CRNA",
            "residentLevel": None,
            "shiftType": "CRNA-7a-8p",
            "availablePast5pm": True,
            "restrictions": [],
            "affinities": ["L3"],
            "daytimeOR": 63,
            "alreadyDeployed": False
        },
        {
            "name": "Dr. Kim",
            "role": "resident",
            "residentLevel": "R3",
            "shiftType": "3p-10p",
            "availablePast5pm": True,
            "restrictions": [],
            "affinities": [],
            "daytimeOR": 5,
            "alreadyDeployed": True
        },
        {
            "name": "Dr. Nguyen",
            "role": "resident",
            "residentLevel": "R2",
            "shiftType": "3p-10p",
            "availablePast5pm": True,
            "restrictions": [],
            "affinities": [],
            "daytimeOR": None,
            "alreadyDeployed": False
        }
    ]
}


@click.group()
def main() -> None:
    """MGH Anesthesia 5PM Coverage Planner."""


@main.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path), required=False)
@click.option(
    "--sample",
    is_flag=True,
    default=False,
    help="Run the built-in sample scenario instead of reading a file.",
)
def plan_cmd(input_file: Path | None, sample: bool) -> None:
    """Generate a 5PM coverage plan from INPUT_FILE (JSON).

    \b
    Run with --sample to try the built-in example scenario.
    Run 'reliefplan sample' to write a starter input file.
    """
    if sample:
        import tempfile, os

        tmp = Path(tempfile.mkstemp(suffix=".json")[1])
        tmp.write_text(json.dumps(SAMPLE_INPUT, indent=2))
        _run_plan(tmp)
        tmp.unlink()
        return

    if input_file is None:
        err_console.print(
            "[red]Error:[/red] provide an input file or use --sample to run the example."
        )
        sys.exit(1)

    _run_plan(input_file)


# Register under both names for convenience
main.add_command(plan_cmd, name="plan")


def _run_plan(path: Path) -> None:
    from .loader import load

    try:
        rooms, staff = load(path)
    except ValueError as exc:
        err_console.print(f"[red]Input error:[/red] {exc}")
        sys.exit(1)

    late_rooms = [r for r in rooms if r.is_late_running]
    if not late_rooms:
        console.print("[yellow]No late-running ORs found — nothing to plan.[/yellow]")
        return

    result = plan(rooms, staff)
    render_plan(result, rooms, staff)


@main.command()
@click.argument("output_file", type=click.Path(path_type=Path), default="sample_input.json")
def sample(output_file: Path) -> None:
    """Write a starter JSON input file to OUTPUT_FILE (default: sample_input.json)."""
    output_file.write_text(json.dumps(SAMPLE_INPUT, indent=2))
    console.print(f"[green]Sample input written to[/green] {output_file}")
    console.print("Edit it, then run: [bold]reliefplan plan sample_input.json[/bold]")


@main.command()
@click.argument("input_file", type=click.Path(exists=True, path_type=Path))
def validate(input_file: Path) -> None:
    """Validate an input file without running the algorithm."""
    try:
        rooms, staff = load(input_file)
    except ValueError as exc:
        err_console.print(f"[red]Validation failed:[/red] {exc}")
        sys.exit(1)

    late = sum(1 for r in rooms if r.is_late_running)
    avail_att = sum(1 for s in staff if s.role == "attending" and s.available_past_5pm)
    avail_crna = sum(1 for s in staff if s.role == "CRNA" and s.available_past_5pm)
    avail_res = sum(1 for s in staff if s.role == "resident" and s.available_past_5pm)

    console.print(f"[green]✓ Input is valid.[/green]")
    console.print(f"  Rooms total: {len(rooms)} ({late} late-running)")
    console.print(f"  Attendings available after 5pm: {avail_att}")
    console.print(f"  CRNAs available after 5pm: {avail_crna}")
    console.print(f"  Residents available after 5pm: {avail_res}")


@main.command()
@click.option("--host", default="127.0.0.1", help="Bind host.", show_default=True)
@click.option("--port", default=8000, help="Port to listen on.", show_default=True, type=int)
@click.option("--reload", is_flag=True, help="Auto-reload on code changes (development).")
def serve(host: str, port: int, reload: bool) -> None:
    """Start the web interface in a browser-accessible server."""
    try:
        import uvicorn
    except ImportError:
        err_console.print(
            "[red]uvicorn is not installed.[/red] Run: pip install 'reliefplan[web]'"
        )
        sys.exit(1)

    console.print(
        f"Starting web interface at [bold]http://{host}:{port}[/bold]  "
        "(Ctrl+C to stop)"
    )
    uvicorn.run("reliefplan.api:app", host=host, port=port, reload=reload)
