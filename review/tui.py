"""
Interactive TUI for reviewing submissions one by one.

Shows score breakdown, opens submission file, and lets you approve or override scores.
Press 'e' to edit individual criterion scores, 'r' to open the rubric, 'b' to go back.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import openpyxl
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text
from review.tui_components import (
    find_submission_file,
    load_review_data,
    needs_review_check,
    open_file,
    auto_approve_students,
    ReviewSession,
    handle_approve,
    handle_edit,
    handle_notes,
    handle_override,
)


def display_student(console: Console, row_data: dict, row_idx: int, total: int, breakdown: list | None = None) -> list:
    """Display student info and score breakdown using Rich. Returns the breakdown list."""
    console.print()
    console.print(Panel.fit(
        f"[bold cyan]Student {row_idx}/{total}[/bold cyan]",
        title="Review Progress",
        border_style="blue"
    ))
    info_table = Table(show_header=False, box=None)
    info_table.add_row("[bold]Student ID:[/]", str(row_data["student_id"]))
    info_table.add_row("[bold]Name:[/]", str(row_data["student_name"]))

    # Show override score if present
    override_score = row_data.get("reviewer_override_score")
    if override_score is not None and override_score != "":
        try:
            override_val = float(override_score)
            total_max = float(row_data["total_max"])
            override_pct = round((override_val / total_max) * 100, 1) if total_max > 0 else 0
            info_table.add_row("[bold]Score:[/]", f"[red][strike]{row_data['total_score']} / {row_data['total_max']} ({row_data['pct']}%)[/strike][/red]")
            info_table.add_row("[bold]Override:[/]", f"[green]{override_val} / {total_max} ({override_pct}%)[/green]")
        except (ValueError, TypeError):
            info_table.add_row("[bold]Score:[/]", f"{row_data['total_score']} / {row_data['total_max']} ({row_data['pct']}%)")
    else:
        info_table.add_row("[bold]Score:[/]", f"{row_data['total_score']} / {row_data['total_max']} ({row_data['pct']}%)")

    info_table.add_row("[bold]Confidence:[/]", f"{row_data['confidence']}")
    info_table.add_row("[bold]Needs review:[/]", "[yellow]YES[/yellow]" if row_data["needs_review"] == "YES" else "NO")
    info_table.add_row("[bold]Current approved:[/]", "[green]YES[/green]" if str(row_data.get("approved", "")).upper() == "YES" else "[red]NO[/red]")
    current_notes = str(row_data.get("reviewer_notes") or "")
    if current_notes:
        info_table.add_row("[bold]Reviewer notes:[/]", f"[green]{current_notes}[/green]")
    else:
        info_table.add_row("[bold]Reviewer notes:[/]", "[dim][yellow](none)[/yellow][/dim]")
    console.print(Panel(info_table, title="Student Info", border_style="cyan"))

    if breakdown is None:
        try:
            breakdown = json.loads(row_data.get("breakdown_json", "[]"))
        except json.JSONDecodeError:
            breakdown = []
            console.print("[yellow]Warning: Could not parse breakdown_json[/yellow]")

    if breakdown:
        bd_table = Table(title="Score Breakdown")
        bd_table.add_column("#", style="dim", justify="right")
        bd_table.add_column("Criterion", style="cyan")
        bd_table.add_column("Awarded", justify="right", style="green")
        bd_table.add_column("Max", justify="right")
        bd_table.add_column("Reasoning", style="dim")
        for i, item in enumerate(breakdown, start=1):
            awarded = float(item.get("points_awarded", 0))
            max_p = float(item.get("points_max", 0))
            style = "red" if awarded < max_p else "green"
            bd_table.add_row(
                str(i),
                str(item.get("criterion", "")),
                Text(f"{awarded}", style=style),
                f"{max_p}",
                str(item.get("reasoning", ""))
            )
        console.print(Panel(bd_table, border_style="green"))

    try:
        uncertain = json.loads(row_data.get("uncertain_parts_json", "[]"))
        if uncertain:
            uc_table = Table(title="Uncertain Parts")
            uc_table.add_column("Description", style="yellow")
            uc_table.add_column("Suggested Score", justify="right")
            for item in uncertain:
                uc_table.add_row(
                    str(item.get("description", "")),
                    f"{item.get('suggested_score', 0)} / {item.get('suggested_max', 0)}"
                )
            console.print(Panel(uc_table, border_style="yellow"))
    except json.JSONDecodeError:
        pass

    if row_data.get("llm_reasoning"):
        console.print(Panel(
            Text(str(row_data["llm_reasoning"]), style="dim"),
            title="LLM Reasoning",
            border_style="dim"
        ))

    return breakdown


def run_review_tui(
    console: Console,
    scores: Path,
    submissions: Path = Path("submissions"),
    rubric: Path = Path("rubric.md"),
    needs_review_only: bool = False,
    all_students: bool = False,
    auto_approve: bool = False,
) -> None:
    """Interactive TUI for reviewing submissions one by one."""
    if not scores.exists():
        console.print(f"[red]Error:[/red] Scores file not found: {scores}")
        raise FileNotFoundError(f"Scores file not found: {scores}")

    if rubric.exists():
        console.print(f"[bold blue]Rubric:[/bold blue] {rubric}")
    else:
        console.print(f"[yellow]Warning: Rubric file not found: {rubric}[/yellow]")

    console.print(f"[bold]Loading spreadsheet:[/bold] {scores}")
    session = ReviewSession(scores, needs_review_only, all_students)

    if not session.rows:
        console.print("[yellow]No students to review with the current filters.[/yellow]")
        return

    console.print(f"[green]Found {len(session.rows)} student(s) to review.[/green]")

    if auto_approve:
        if auto_approve_students(session.ws, session.idx, console):
            # Save the changes before reloading
            session.wb.save(scores)
            session = ReviewSession(scores, needs_review_only, all_students)
            console.print(f"[green]{len(session.rows)} student(s) remaining to review after auto-approve.[/green]")

    try:
        while True:
            current_row = session.get_current_row()
            if current_row is None:
                break

            row_idx, row_data = current_row
            i = session.current_idx + 1

            try:
                breakdown = json.loads(row_data.get("breakdown_json", "[]"))
            except json.JSONDecodeError:
                breakdown = []

            display_student(console, row_data, i, len(session.rows), breakdown)
            student_id = str(row_data["student_id"])
            student_name = str(row_data["student_name"])
            sub_file = find_submission_file(submissions, student_id, student_name)

            if sub_file:
                console.print(f"[bold blue]Submission:[/bold blue] {sub_file}")
            else:
                console.print("[yellow]Warning: No submission file found[/yellow]")
            if rubric.exists():
                console.print(f"[bold blue]Rubric:[/bold blue] {rubric}")
            console.print()

            choices = ["a", "approve", "s", "skip", "e", "edit", "n", "notes", "o", "open", "r", "rubric", "ov", "override", "q", "quit"]
            if session.current_idx > 0:
                choices.extend(["b", "back"])

            action = Prompt.ask(
                "[bold cyan]Action[/bold cyan] [dim](a)pprove (s)kip (e)dit (n)otes (o)pen (r)ubric (ov)erride (b)ack (q)uit[/dim]",
                choices=choices,
                default="skip",
                show_choices=False,
            )

            if action in ("q", "quit"):
                console.print("[yellow]Quitting...[/yellow]")
                break

            if action in ("b", "back"):
                session.prev_student()
                continue

            if action in ("a", "approve"):
                if handle_approve(session, row_idx, row_data, console):
                    session.next_student()
            elif action in ("n", "notes"):
                handle_notes(session, row_idx, row_data, console)
            elif action in ("e", "edit"):
                breakdown = handle_edit(session, row_idx, row_data, breakdown, console)
            elif action in ("o", "open") and sub_file:
                open_file(sub_file, console)
            elif action in ("r", "rubric"):
                if rubric.exists():
                    open_file(rubric, console)
                else:
                    console.print(f"[yellow]Rubric file not found: {rubric}[/yellow]")
            elif action in ("ov", "override"):
                handle_override(session, row_idx, row_data, console)
            else: # skip
                console.print("[dim]Skipped.[/dim]")
                session.next_student()

    except KeyboardInterrupt:
        console.print("\n[yellow]\nInterrupted by user (Ctrl-C)[/yellow]")

    if session.modified:
        console.print()
        if Confirm.ask("[bold cyan]Save changes to spreadsheet?[/bold cyan]", default=True):
            session.save_changes()
            console.print(f"[green]Saved to {scores}[/green]")
        else:
            console.print("[yellow]Changes not saved.[/yellow]")
    else:
        console.print("[dim]No changes made.[/dim]")

