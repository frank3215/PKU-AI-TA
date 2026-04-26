"""
Iterate: quick re-score a specific student after rubric/prompt changes.

Usage:
    uv run python iterate.py <student_id> [--rubric rubric.md] [--prompt prompts/system_zh.md] [--assignment 424464] [--scores scores.xlsx]

The script will:
  1. Load the student's submission (from cache/ or fetch from course.pku.edu.cn)
  2. Score it with the current rubric and prompt
  3. Display the full result (breakdown, notes, reasoning)
  4. Optionally diff against an existing entry in scores.xlsx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


def _load_existing_result(scores_path: Path, student_id: str):
    """Load a student's existing result from scores.xlsx if present."""
    if not scores_path.exists():
        return None
    try:
        from review.spreadsheet import load_reviewed
        records = load_reviewed(scores_path)
        for r in records:
            if r.result.student_id == student_id:
                return r.result
    except Exception:
        pass
    return None


def _display_result(result, title: str = "Result"):
    """Pretty-print a ScoringResult."""
    color = "yellow" if result.needs_review else "green"
    status = "NEEDS REVIEW" if result.needs_review else "OK"

    console.print()
    console.print(Panel(
        f"[bold]{result.student_id}[/] {result.student_name}  →  "
        f"[bold {color}]{result.total_score:.0f}/{result.total_max:.0f}[/] ({result.pct:.0f}%)  [{color}]{status}[/{color}]",
        title=title,
        border_style=color,
    ))

    # Breakdown table
    bd_table = Table(show_header=True, header_style="bold", box="simple")
    bd_table.add_column("#", justify="right", width=3)
    bd_table.add_column("Criterion", min_width=20)
    bd_table.add_column("Score", justify="right", width=10)
    bd_table.add_column("Max", justify="right", width=10)
    bd_table.add_column("Reasoning")

    for i, b in enumerate(result.breakdown, 1):
        score_color = "red" if b.points_awarded < b.points_max else "green"
        bd_table.add_row(
            str(i),
            b.criterion,
            f"[{score_color}]{b.points_awarded:.0f}[/{score_color}]",
            str(int(b.points_max)),
            b.reasoning.strip(),
        )
    console.print(bd_table)

    # Uncertain parts
    if result.uncertain_parts:
        console.print("\n[yellow]Uncertain parts:[/yellow]")
        for up in result.uncertain_parts:
            console.print(f"  [yellow]⚠[/yellow] [dim]{up.description.strip()}[/]")

    # Student feedback (reviewer notes)
    if result.student_feedback:
        console.print(f"\n[cyan]Student feedback:[/cyan]")
        console.print(Panel(result.student_feedback.strip(), border_style="cyan"))

    # LLM reasoning
    if result.llm_reasoning:
        console.print(f"\n[dim]LLM reasoning:[/dim]")
        console.print(result.llm_reasoning.strip())


def _display_diff(old, new):
    """Show a side-by-side diff of two ScoringResults."""
    console.print("\n[bold]Diff (Old → New)[/bold]")

    diff_table = Table(show_header=True, header_style="bold")
    diff_table.add_column("Criterion")
    diff_table.add_column("Old Score", justify="right")
    diff_table.add_column("New Score", justify="right")
    diff_table.add_column("Change", justify="center")

    old_map = {b.criterion: b for b in old.breakdown}
    new_map = {b.criterion: b for b in new.breakdown}
    all_keys = list(dict.fromkeys(list(old_map.keys()) + list(new_map.keys())))

    for k in all_keys:
        ob = old_map.get(k)
        nb = new_map.get(k)
        old_s = f"{ob.points_awarded:.0f}" if ob else "—"
        new_s = f"{nb.points_awarded:.0f}" if nb else "—"
        if ob and nb:
            if nb.points_awarded > ob.points_awarded:
                change = "[green]↑[/green]"
            elif nb.points_awarded < ob.points_awarded:
                change = "[red]↓[/red]"
            else:
                change = "[dim]=[/dim]"
        else:
            change = "[yellow]*[/yellow]"
        diff_table.add_row(k or "?", old_s, new_s, change)

    diff_table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{old.total_score:.0f}[/bold]",
        f"[bold]{new.total_score:.0f}[/bold]",
        "",
    )
    console.print(diff_table)

    # Notes diff
    if old.student_feedback != new.student_feedback:
        console.print("\n[bold magenta]Notes changed[/bold magenta]")
        console.print("[dim]Old:[/dim]")
        console.print(Panel(old.student_feedback or "(none)", border_style="dim"))
        console.print("[cyan]New:[/cyan]")
        console.print(Panel(new.student_feedback or "(none)", border_style="cyan"))


def main():
    parser = argparse.ArgumentParser(description="Iterate: quick re-score one student")
    parser.add_argument("student_id", help="Student ID to re-score")
    parser.add_argument("--rubric", type=Path, default=Path("rubric.md"), help="Rubric file")
    parser.add_argument("--prompt", type=Path, default=Path("prompts/system_zh.md"), help="System prompt file")
    parser.add_argument("--assignment", type=str, default="", help="Assignment gradeBookPK (required if fetching online)")
    parser.add_argument("--course", type=str, default="", help="Course ID (required if fetching online)")
    parser.add_argument("--scores", type=Path, default=Path("scores.xlsx"), help="Existing scores file to diff against")
    parser.add_argument("--cache", type=Path, default=Path("submissions"), help="Cache directory")
    parser.add_argument("--fetch", action="store_true", help="Force fetch from online even if cached")
    args = parser.parse_args()

    if not args.rubric.exists():
        console.print(f"[red]Rubric not found:[/red] {args.rubric}")
        sys.exit(1)
    if not args.prompt.exists():
        console.print(f"[red]Prompt not found:[/red] {args.prompt}")
        sys.exit(1)

    rubric_text = args.rubric.read_text(encoding="utf-8")

    # --- Load submission ---
    submission = None

    # Try cache first
    if args.cache.exists() and not args.fetch:
        for subdir in args.cache.iterdir():
            if subdir.is_dir():
                for f in subdir.iterdir():
                    if f.name.startswith(args.student_id + "_"):
                        console.print(f"[dim]Loading cached submission: {f}[/dim]")
                        from models import Attachment, Submission
                        data = f.read_bytes()
                        content_type = "application/pdf" if f.suffix == ".pdf" else "application/octet-stream"
                        submission = Submission(
                            student_id=args.student_id,
                            student_name=f.name[len(args.student_id)+1 : f.name.rfind(f.suffix)],
                            bb_user_id="",
                            attachments=[Attachment(filename=f.name, content_type=content_type, data=data)],
                            submitted_at="",
                            already_graded=False,
                        )
                        break
            if submission:
                break

    if not submission and args.course and args.assignment:
        console.print("[bold]Fetching submission from course.pku.edu.cn…[/bold]")
        from auth.iaaa import get_session
        from crawler.pku_homework import PKUHomeworkCrawler

        client = get_session()
        crawler = PKUHomeworkCrawler(client, args.course, {args.student_id})

        # Need to look up assignment title
        assignments = crawler.fetch_assignments()
        title = ""
        for a in assignments:
            if a.get("gradeBookPK") == args.assignment or a.get("id") == f"_{args.assignment}_1":
                title = a.get("name", "")
                break

        subs = crawler.fetch_submissions(args.assignment, title, cache_dir=args.cache)
        for s in subs:
            if s.student_id == args.student_id:
                submission = s
                break

    if not submission:
        console.print(
            f"[red]Could not find submission for {args.student_id}.[/red]\n"
            "Either provide --course and --assignment to fetch online,\n"
            "or ensure the file exists under submissions/<assignment>/"
        )
        sys.exit(1)

    # --- Score ---
    console.print(f"\n[bold]Scoring with rubric:[/bold] {args.rubric.name}  |  prompt: {args.prompt.name}")
    from scorer.llm import score_submission
    new_result = score_submission(submission, rubric_text, args.prompt)

    _display_result(new_result, title="New Score")

    # --- Diff against existing ---
    old_result = _load_existing_result(args.scores, args.student_id)
    if old_result:
        _display_diff(old_result, new_result)
    else:
        console.print(f"\n[dim]No existing entry in {args.scores} to diff against.[/dim]")

    # --- Option to save ---
    console.print()
    from rich.prompt import Confirm
    if Confirm.ask("Save this result to a JSON file?", default=False):
        out_path = Path(f"iterate_{args.student_id}.json")
        out_path.write_text(json.dumps(new_result.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[green]Saved to {out_path}[/green]")


if __name__ == "__main__":
    main()
