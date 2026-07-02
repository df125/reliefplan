"""Rich-based terminal output for the coverage plan."""
from __future__ import annotations

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .models import CoveragePlan, CoverageWarning, OperatingRoom, StaffMember

console = Console(width=120)


def _severity_color(severity: str) -> str:
    return {"error": "red", "warning": "yellow", "info": "cyan"}.get(severity, "white")


def _attending_role_color(role: str) -> str:
    return "green" if role == "solo" else "blue"


def render_plan(
    plan: CoveragePlan,
    rooms: list[OperatingRoom],
    staff: list[StaffMember],
) -> None:
    """Print the full coverage plan to the terminal."""
    console.print()
    console.rule("[bold white]MGH ANESTHESIA — 5PM COVERAGE PLAN[/bold white]")
    console.print()

    _render_assignment_board(plan, rooms)
    console.print()
    _render_supervisor_groups(plan, rooms, staff)
    console.print()
    _render_relief_summary(plan, rooms)
    console.print()
    _render_warnings(plan)


def _render_assignment_board(plan: CoveragePlan, rooms: list[OperatingRoom]) -> None:
    room_map = {r.id: r for r in rooms}

    tbl = Table(
        title="Assignment Board",
        box=box.ROUNDED,
        show_lines=True,
        title_style="bold",
        min_width=110,
    )
    tbl.add_column("OR", style="bold white", justify="center", min_width=4)
    tbl.add_column("Bldg", min_width=7)
    tbl.add_column("Floor", min_width=8)
    tbl.add_column("Est. End", min_width=8)
    tbl.add_column("Flags", min_width=7)
    tbl.add_column("Attending", min_width=20)
    tbl.add_column("Role", min_width=8)
    tbl.add_column("Physical Provider", min_width=22)
    tbl.add_column("Cont", justify="center", min_width=4)

    # Sort by building then floor then or id
    floor_order = {"THOR": 0, "Gray": 1, "Jackson": 2, "L2": 3, "L3": 4, "L4": 5}
    sorted_asgn = sorted(
        plan.assignments,
        key=lambda a: (
            room_map[a.or_id].building,
            floor_order.get(room_map[a.or_id].floor, 99),
            a.or_id,
        ),
    )

    for asgn in sorted_asgn:
        room = room_map[asgn.or_id]
        flags = ""
        if room.flagged_complex:
            flags += "[yellow]CPX[/yellow] "
        if room.flagged_fluoro:
            flags += "[magenta]FLU[/magenta]"
        if room.is_new_start:
            flags += "[cyan]NEW[/cyan]"

        role_label = "SOLO" if asgn.attending_role == "solo" else "SUPV"
        role_text = Text(role_label, style=_attending_role_color(asgn.attending_role))

        if asgn.physical_provider:
            prov_text = f"{asgn.physical_provider} ({asgn.physical_provider_type})"
        else:
            prov_text = "[dim]— solo attending —[/dim]"

        cont_mark = "[green]✓[/green]" if asgn.continuity else ""

        tbl.add_row(
            str(room.id),
            room.building,
            room.floor,
            room.estimated_end or "?",
            flags.strip() or "—",
            asgn.attending,
            role_text,
            prov_text,
            cont_mark,
        )

    # Add unassigned rows
    assigned_ids = {a.or_id for a in plan.assignments}
    for or_id in plan.unassigned_rooms:
        if or_id in assigned_ids:
            continue
        room = room_map.get(or_id)
        if room:
            tbl.add_row(
                str(or_id),
                room.building,
                room.floor,
                room.estimated_end or "?",
                "",
                "[red bold]UNASSIGNED[/red bold]",
                "",
                "",
                "",
            )

    console.print(tbl)


def _render_supervisor_groups(
    plan: CoveragePlan,
    rooms: list[OperatingRoom],
    staff: list[StaffMember],
) -> None:
    room_map = {r.id: r for r in rooms}
    staff_map = {s.name: s for s in staff}

    tbl = Table(
        title="Supervisor Groups",
        box=box.SIMPLE_HEAVY,
        title_style="bold",
        show_lines=False,
    )
    tbl.add_column("Attending", width=22, style="bold")
    tbl.add_column("Shift", width=9)
    tbl.add_column("Role", width=11)
    tbl.add_column("Rooms", min_width=40)
    tbl.add_column("Load", width=8, justify="center")

    for att_name, or_ids in sorted(plan.supervisor_groups.items()):
        att = staff_map.get(att_name)
        shift = att.shift_type if att else "?"
        # Determine role from assignment list
        roles = {a.attending_role for a in plan.assignments if a.attending == att_name}
        role_str = "solo" if "solo" in roles else "supervisor"

        room_descs = []
        for or_id in sorted(or_ids):
            room = room_map.get(or_id)
            if room:
                room_descs.append(f"OR{or_id}({room.floor})")
            else:
                room_descs.append(f"OR{or_id}")

        tbl.add_row(
            att_name,
            shift,
            role_str,
            ", ".join(room_descs),
            str(len(or_ids)),
        )

    console.print(tbl)


def _render_relief_summary(plan: CoveragePlan, rooms: list[OperatingRoom]) -> None:
    if not plan.relief_entries:
        console.print("[dim]No relief needed (all daytime providers are on the after-5pm list).[/dim]")
        return

    tbl = Table(
        title="Relief Summary (providers leaving at 5PM)",
        box=box.SIMPLE_HEAVY,
        title_style="bold",
    )
    tbl.add_column("OR", width=6, justify="center")
    tbl.add_column("Outgoing Provider", width=24)
    tbl.add_column("Role", width=12)

    for entry in sorted(plan.relief_entries, key=lambda e: (e.or_id, e.relieved_role)):
        tbl.add_row(str(entry.or_id), entry.relieved_name, entry.relieved_role)

    console.print(tbl)


def _render_warnings(plan: CoveragePlan) -> None:
    if not plan.warnings:
        console.print("[green]✓ No warnings.[/green]")
        return

    errors = [w for w in plan.warnings if w.severity == "error"]
    warnings = [w for w in plan.warnings if w.severity == "warning"]
    infos = [w for w in plan.warnings if w.severity == "info"]

    def _print_group(items: list[CoverageWarning], label: str, color: str) -> None:
        if not items:
            return
        console.print(f"[{color} bold]{label}[/{color} bold]")
        for w in items:
            prefix = f"OR {w.or_id}: " if w.or_id else ""
            console.print(f"  [{color}]•[/{color}] {prefix}{w.message}")
        console.print()

    _print_group(errors, "ERRORS (hard rule violations)", "red")
    _print_group(warnings, "WARNINGS (soft preference deviations)", "yellow")
    _print_group(infos, "INFO (optimisations applied)", "cyan")
