"""
PKU AI Teaching Assistant CLI

Commands:
  ta grade   --course <id> --column <id> --rubric <file> [--whitelist a,b,c] [--out scores.xlsx] [--verbose] [--resume] [--lang en|zh]
  ta review  [--scores scores.xlsx] [--submissions submissions/] [--needs-review] [--all]
  ta submit  --course <id> --column <id> --scores <reviewed.xlsx> [--dry-run]
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from pathlib import Path
from typing import Annotated, Optional
from time import time
import zipfile

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn, TextColumn

app = typer.Typer(help="PKU AI Teaching Assistant")
console = Console()


class DaemonThreadPoolExecutor(ThreadPoolExecutor):
    """ThreadPoolExecutor whose worker threads are daemonic.

    This prevents the interpreter from hanging on exit when worker threads
    are blocked on long-running I/O (e.g. LLM API calls).
    """

    def _adjust_thread_count(self) -> None:
        _orig = threading.Thread
        class _DaemonThread(_orig):
            def __init__(self, *args, **kwargs):
                kwargs.setdefault("daemon", True)
                super().__init__(*args, **kwargs)
        threading.Thread = _DaemonThread  # type: ignore[misc]
        try:
            super()._adjust_thread_count()
        finally:
            threading.Thread = _orig


def _interactive_grade_setup(
    course: str,
    column: str,
    rubric: Path,
    whitelist: str,
    out: Path,
    save_dir: Optional[Path],
    prompt_file: Path,
    threads: int,
    due_date: Optional[str],
    console: Console,
) -> tuple[str, str, Path, str, Path, Optional[Path], Path, int, Optional[str]]:
    """Interactively prompt for grade parameters with .env defaults."""
    from config import settings
    from review.tui_components import prompt_text

    # --- course ---
    course_id = course or settings.course_id
    if not course_id:
        course_id = prompt_text("Course ID (e.g. _12345_1)", default="", console=console)
    if not course_id:
        console.print("[red]Error: course_id is required[/red]")
        raise typer.Exit(1)

    # --- whitelist (load early so assignment counts are filtered) ---
    whitelist_default = whitelist or settings.student_whitelist
    student_list_path = Path("student_list")
    if not whitelist_default and student_list_path.exists():
        student_list_content = ",".join(
            line.strip() for line in student_list_path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
        if student_list_content:
            console.print(f"  [dim]Auto-loaded {student_list_content.count(',') + 1} student(s) from student_list[/dim]")
            whitelist = student_list_content
    # If still empty, ask interactively (only if --column is not provided, i.e. we need to show assignments)
    if not column and not whitelist:
        if whitelist_default:
            whitelist_input = prompt_text(
                "Student whitelist (comma-separated, empty = all students)",
                default=whitelist_default,
                console=console,
            )
            whitelist = whitelist_input or ""
        else:
            whitelist_input = prompt_text(
                "Student whitelist (comma-separated, empty = all students)",
                default="",
                console=console,
            )
            whitelist = whitelist_input or ""

    whitelist_ids: set[str] = {s.strip() for s in whitelist.split(",") if s.strip()} if whitelist else set()

    # --- column (fetch assignments if missing) ---
    if not column:
        from auth.iaaa import get_session
        from crawler.pku_homework import PKUHomeworkCrawler
        from rich.table import Table

        console.print("\n[bold]Authenticating with PKU IAAA…[/bold]")
        client = get_session()
        crawler = PKUHomeworkCrawler(client, course_id, whitelist_ids)
        assignments = crawler.fetch_assignments()

        if not assignments:
            console.print("[yellow]No assignments found.[/yellow]")
            raise typer.Exit(0)

        table = Table(title=f"Assignments ({len(assignments)})")
        table.add_column("#", justify="right", style="dim")
        table.add_column("Name", style="cyan")
        table.add_column("gradeBookPK (--column)", style="green", no_wrap=True)
        table.add_column("Submitted", justify="right")
        table.add_column("Graded", justify="right")
        table.add_column("Ungraded", justify="right")
        for i, a in enumerate(assignments, 1):
            pk = a.get("gradeBookPK") or a["id"].strip("_").split("_")[0]
            name = a.get("name", "")
            try:
                counts = crawler.count_submissions(pk, name)
                submitted = str(counts["total"])
                graded = f"[green]{counts['graded']}[/green]" if counts["graded"] else "0"
                ungraded = f"[yellow]{counts['ungraded']}[/yellow]" if counts["ungraded"] else "0"
            except Exception:
                submitted = "err"
                graded = "err"
                ungraded = "err"
            table.add_row(str(i), name, pk, submitted, graded, ungraded)
        console.print(table)

        choice = prompt_text("Select assignment (number or gradeBookPK)", default="", console=console)
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(assignments):
                column = assignments[idx].get("gradeBookPK") or assignments[idx]["id"].strip("_").split("_")[0]
            else:
                column = choice
        except ValueError:
            column = choice
    if not column:
        console.print("[red]Error: --column is required[/red]")
        raise typer.Exit(1)

    # --- rubric ---
    if not rubric.exists():
        candidates = [p for p in Path(".").iterdir() if p.is_file() and "rubric" in p.name.lower()]
        if not candidates:
            candidates = sorted([p for p in Path(".").glob("*.md") if p.is_file()], key=lambda p: p.name)
        if candidates:
            console.print("\n[yellow]Rubric file not found.[/yellow] Available candidates:")
            for i, c in enumerate(candidates[:10], 1):
                marker = " [green](recommended)[/green]" if "rubric" in c.name.lower() else ""
                console.print(f"  {i}. {c}{marker}")
            choice = prompt_text("Select rubric (number or path)", default="", console=console)
            try:
                rubric = candidates[int(choice) - 1]
            except (ValueError, IndexError):
                rubric = Path(choice) if choice else rubric
    if rubric.exists():
        confirmed = prompt_text("Rubric file", default=str(rubric), console=console)
        rubric = Path(confirmed) if confirmed else rubric
    if not rubric.exists():
        console.print(f"[red]Error: Rubric file not found: {rubric}[/red]")
        raise typer.Exit(1)

    # --- out ---
    out_str = prompt_text("Output Excel file", default=str(out), console=console)
    out = Path(out_str) if out_str else out

    # --- save_dir ---
    save_dir_default = str(save_dir) if save_dir else "submissions"
    save_dir_str = prompt_text("Submission save directory", default=save_dir_default, console=console)
    save_dir = Path(save_dir_str) if save_dir_str else None

    # --- prompt ---
    prompt_str = prompt_text("System prompt file", default=str(prompt_file), console=console)
    prompt_file = Path(prompt_str) if prompt_str else prompt_file

    # --- threads ---
    threads_default = str(threads if threads > 0 else settings.ta_threads)
    threads_str = prompt_text("Parallel scoring threads", default=threads_default, console=console)
    try:
        threads = int(threads_str) if threads_str else (threads if threads > 0 else settings.ta_threads)
    except ValueError:
        threads = settings.ta_threads

    # --- due_date ---
    due_str = prompt_text("Due date (ISO 8601, empty = auto-fetch from Blackboard)", default=due_date or "", console=console)
    due_date = due_str or due_date

    return course_id, column, rubric, whitelist, out, save_dir, prompt_file, threads, due_date


@app.command()
def grade(
    course: Annotated[str, typer.Option(help="Blackboard course ID, e.g. _12345_1")] = "",
    column: Annotated[str, typer.Option(help="Gradebook column (assignment) ID")] = "",
    rubric: Annotated[Path, typer.Option(help="Path to rubric file (any format the LLM supports)")] = Path("rubric.md"),
    whitelist: Annotated[str, typer.Option(help="Comma-separated student IDs to include; empty = all")] = "",
    out: Annotated[Path, typer.Option(help="Output Excel path")] = Path("scores.xlsx"),
    save_dir: Annotated[Optional[Path], typer.Option(help="Save submission files here for human review; default: submissions/")] = Path("submissions"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show intermediate scores for each student")] = False,
    resume: Annotated[bool, typer.Option("--resume", "-r", help="Resume from previous partial run (if any)")] = False,
    regrade_unapproved: Annotated[bool, typer.Option("--regrade-unapproved", help="Keep approved students, only regrade those not approved")] = False,
    prompt: Annotated[Path, typer.Option(help="System prompt file for the LLM (default: prompts/system_zh.md)")] = Path("prompts/system_zh.md"),
    threads: Annotated[int, typer.Option(help="Parallel scoring threads (default: from .env or 4)")] = 0,
    due_date: Annotated[Optional[str], typer.Option(help="Assignment due date (ISO 8601, e.g. 2026-03-22T15:59:00). Auto-fetched from Blackboard if omitted.")] = None,
    interactive: Annotated[bool, typer.Option("--interactive", "-i", help="Interactively prompt for missing parameters")] = False,
    auto_submit: Annotated[bool, typer.Option("--auto-submit", help="After grading, automatically submit any already-approved scores")] = False,
    limit: Annotated[int, typer.Option(help="Only grade the first N submissions (useful for testing or batching); 0 = no limit")] = 0,
) -> None:
    """Crawl submissions, score with LLM, export review spreadsheet.

    Press Ctrl-C to interrupt; partial results will be saved to the output file
    and can be resumed with --resume.

    Use --regrade-unapproved to keep already-approved students and only regrade
    those that haven't been approved yet.

    Use --interactive (or -i) to interactively fill in missing parameters.

    Use --auto-submit to automatically submit already-approved scores after
    grading completes (or after a Ctrl-C save).

    Use --limit N to grade only the first N submissions. Good for testing a
    rubric on a small sample before committing to a full run.
    """
    from threading import Lock

    from auth.iaaa import get_session
    from config import settings
    from crawler.pku_homework import PKUHomeworkCrawler
    from review.spreadsheet import export, load_reviewed
    from scorer.llm import score_submission
    from submitter.blackboard import submit_scores
    from models import CriterionScore, ScoringResult

    # Override thread count from CLI if explicitly set
    if threads > 0:
        settings.ta_threads = threads

    # Merge --auto-submit CLI flag with .env config (CLI takes precedence)
    if not auto_submit:
        auto_submit = settings.auto_submit

    # Checkpoint save/load using Excel format
    checkpoint_path = out
    all_results: list[ScoringResult] = []
    processed_ids: set[str] = set()
    save_lock = Lock()

    def save_checkpoint() -> None:
        """Save current progress to output Excel file."""
        with save_lock:
            if all_results:
                export(all_results, checkpoint_path)

    def _try_auto_submit() -> None:
        """If --auto-submit is enabled and there are approved records, submit them."""
        if not auto_submit or not checkpoint_path.exists():
            return
        try:
            records = load_reviewed(checkpoint_path)
            approved = [r for r in records if r.approved]
            if not approved:
                return
            col_id = column if column.startswith("_") else f"_{column}_1"
            console.print(f"\n[bold]Auto-submitting {len(approved)} approved score(s)…[/bold]")
            submit_scores(client, course_id, col_id, approved, dry_run=False)
        except Exception as e:
            console.print(f"[yellow]Auto-submit skipped:[/yellow] {e}")

    def load_checkpoint() -> tuple[list[ScoringResult], set[str]]:
        """Load previous progress from output Excel file (if exists and --resume or --regrade-unapproved is set)."""
        if (resume or regrade_unapproved) and checkpoint_path.exists():
            # Sanity check: empty or truncated Excel files cannot be loaded
            if checkpoint_path.stat().st_size < 100:
                console.print(
                    f"[yellow]Warning:[/yellow] Checkpoint file {checkpoint_path} is empty ({checkpoint_path.stat().st_size} bytes). "
                    "A previous run may have been interrupted during save.\n"
                    f"[dim]Hint: restore from a backup (e.g. scores_副本.xlsx) if available.[/dim]"
                )
                return [], set()

            try:
                from review.spreadsheet import load_reviewed
                records = load_reviewed(checkpoint_path)

                if regrade_unapproved:
                    # Keep only approved students, others will be regraded
                    approved_results = [r.result for r in records if r.approved]
                    all_results_loaded = [r.result for r in records]
                    console.print(f"[bold cyan]Regrade mode:[/bold cyan] Loaded {len(all_results_loaded)} total, keeping {len(approved_results)} already-approved")
                    return approved_results, {r.student_id for r in approved_results}
                else:
                    # Normal resume: keep all previously processed
                    results = [r.result for r in records]
                    console.print(f"[bold cyan]Resuming from checkpoint:[/bold cyan] {len(results)} previously processed result(s)")
                    return results, {r.student_id for r in results}
            except zipfile.BadZipFile as e:
                console.print(
                    f"[yellow]Warning:[/yellow] Checkpoint file {checkpoint_path} is corrupted ({e}).\n"
                    f"[dim]Hint: restore from a backup (e.g. scores_副本.xlsx) if available.[/dim]"
                )
            except Exception as e:
                console.print(f"[yellow]Warning: Could not load checkpoint: {e}[/yellow]")
        return [], set()

    # Load checkpoint if resuming or regrading unapproved
    unapproved_student_ids: set[str] = set()
    if resume or regrade_unapproved:
        all_results, processed_ids = load_checkpoint()
        if regrade_unapproved and checkpoint_path.exists():
            # For --regrade-unapproved, find students who are NOT approved
            # These are the ones we need to regrade
            try:
                from review.spreadsheet import load_reviewed
                all_records = load_reviewed(checkpoint_path)
                unapproved_student_ids = {r.result.student_id for r in all_records if not r.approved}
                console.print(f"[bold cyan]Regrade mode:[/bold cyan] Found {len(unapproved_student_ids)} unapproved student(s) to regrade")
            except Exception as e:
                console.print(f"[yellow]Warning: Could not determine unapproved students: {e}[/yellow]")
    else:
        all_results = []
        processed_ids = set()

    # Enter interactive mode if explicitly requested or critical params are missing
    course_id = course or settings.course_id
    needs_interactive = interactive or not course_id or not column
    if needs_interactive:
        if interactive:
            console.print("[bold cyan]Interactive mode enabled.[/bold cyan] Press Enter to accept defaults from .env.\n")
        course_id, column, rubric, whitelist, out, save_dir, prompt, threads, due_date = _interactive_grade_setup(
            course=course,  # pass raw CLI value so setup can distinguish .env default from explicit arg
            column=column,
            rubric=rubric,
            whitelist=whitelist,
            out=out,
            save_dir=save_dir,
            prompt_file=prompt,
            threads=threads,
            due_date=due_date,
            console=console,
        )
        # Re-apply thread override after interactive setup
        if threads > 0:
            settings.ta_threads = threads
        checkpoint_path = out

    if not course_id:
        console.print("[red]Error:[/red] --course is required (or set COURSE_ID in .env)")
        raise typer.Exit(1)

    # Build resume command for display on interrupt
    def _build_resume_cmd() -> str:
        parts = [f"--course {course_id}", f"--column {column}", f"--rubric {rubric}"]
        if whitelist:
            parts.append(f"--whitelist {whitelist}")
        if str(out) != "scores.xlsx":
            parts.append(f"--out {out}")
        if save_dir and str(save_dir) != "submissions":
            parts.append(f"--save-dir {save_dir}")
        if verbose:
            parts.append("--verbose")
        if regrade_unapproved:
            parts.append("--regrade-unapproved")
        if str(prompt) != "prompts/system_zh.md":
            parts.append(f"--prompt {prompt}")
        if threads > 0 and threads != settings.ta_threads:
            parts.append(f"--threads {threads}")
        if due_date:
            parts.append(f"--due-date {due_date}")
        if limit > 0:
            parts.append(f"--limit {limit}")
        parts.append("--resume")
        return f"uv run python main.py grade {' '.join(parts)}"

    # Determine whitelist:
    # - If --regrade-unapproved: only regrade unapproved students
    # - Else: use CLI whitelist or settings whitelist
    if regrade_unapproved and unapproved_student_ids:
        whitelist_ids: set[str] = unapproved_student_ids
    else:
        whitelist_ids: set[str] = (
            {s.strip() for s in whitelist.split(",") if s.strip()}
            if whitelist
            else settings.whitelist_ids
        )

    if not rubric.exists():
        console.print(f"[red]Error:[/red] Rubric file not found: {rubric}")
        raise typer.Exit(1)

    rubric_text = rubric.read_text(encoding="utf-8")

    console.print("[bold]Step 1/3:[/bold] Authenticating with PKU IAAA…")
    client = get_session()

    crawler = PKUHomeworkCrawler(client, course_id, whitelist_ids)

    if column:
        # column here is expected to be gradeBookPK (numeric), e.g. "423829"
        columns = [{"gradeBookPK": column, "name": column, "id": f"_{column}_1"}]
    else:
        console.print("[bold]Step 1b:[/bold] Fetching assignment list…")
        columns = crawler.fetch_assignments()
        console.print(f"  Found {len(columns)} assignment(s).")

    interrupted = False
    start_time = time()

    try:
        for col in columns:
            grade_book_pk = col.get("gradeBookPK") or col["id"].strip("_").split("_")[0]
            col_title = col.get("name") or col["id"]
            console.print(f"\n[bold]Step 2/3:[/bold] Fetching submissions for [cyan]{col_title}[/cyan]…")

            # Resolve due date: CLI arg > REST API > skip late-check
            due_dt_str = due_date or crawler.fetch_due_date(grade_book_pk)
            if due_dt_str:
                console.print(f"  Due date: {due_dt_str}")
            else:
                console.print("  [yellow]Warning: could not determine due date — late penalties disabled[/yellow]")

            # Pass skip_ids and limit into fetch_submissions so only the
            # students we actually need are downloaded.
            submissions = crawler.fetch_submissions(
                grade_book_pk, col_title,
                cache_dir=save_dir, verbose=verbose,
                skip_ids=processed_ids, limit=limit,
            )
            if not submissions:
                console.print("  No submissions found (or all skipped by --limit / resume).")
                continue

            if limit > 0 and len(submissions) >= limit:
                console.print(f"  [yellow]--limit {limit}:[/yellow] grading {len(submissions)} submission(s)")

            if save_dir:
                written = _save_submissions(submissions, save_dir, col_title)
                skipped = len(submissions) - written
                msg = f"  Saved files → [cyan]{save_dir / col_title}[/cyan]"
                if skipped:
                    msg += f" ([dim]{skipped} already cached[/dim])"
                console.print(msg)

            total_submissions = len(submissions)
            console.print(f"  Scoring {total_submissions} submission(s) with LLM (threads={settings.ta_threads}, prompt={prompt.name})…")
            console.print(f"  [dim]Press Ctrl-C to interrupt — progress will be saved[/dim]")

            # Use transient=False for verbose mode so results stay on screen
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                TimeElapsedColumn(),
                TextColumn("[dim]ETA: {task.fields[eta]}"),
                TextColumn("[dim]Last: {task.fields[last]}"),
                console=console, transient=not verbose,
            ) as progress:
                task = progress.add_task("  Scoring", total=total_submissions, eta="calculating...", last="—")
                completed_count = 0

                futures: dict = {}
                executor = DaemonThreadPoolExecutor(max_workers=settings.ta_threads)
                try:
                    futures = {executor.submit(score_submission, sub, rubric_text, prompt): sub for sub in submissions}
                    for future in as_completed(futures):
                        sub = futures[future]
                        try:
                            result = future.result()
                            _apply_late_penalty(result, sub, due_dt_str, console)
                            all_results.append(result)
                            processed_ids.add(result.student_id)
                            completed_count += 1

                            # Calculate ETA
                            if completed_count >= 2:
                                elapsed = time() - start_time
                                avg_time_per = elapsed / completed_count
                                remaining = (total_submissions - completed_count) * avg_time_per
                                if remaining < 60:
                                    eta_str = f"{remaining:.0f}s"
                                elif remaining < 3600:
                                    eta_str = f"{remaining/60:.1f}m"
                                else:
                                    eta_str = f"{remaining/3600:.1f}h"
                                progress.update(task, eta=eta_str)
                            else:
                                progress.update(task, eta="...")

                            # Show last-completed result inline (works even in parallel mode)
                            last_status = "[yellow]REVIEW[/yellow]" if result.needs_review else "[green]OK[/green]"
                            last_extra = ""
                            if result.uncertain_parts:
                                last_extra = f" [yellow]⚠{len(result.uncertain_parts)}[/yellow]"
                            progress.update(
                                task,
                                last=f"{result.student_id} {result.total_score:.0f}/{result.total_max:.0f} {last_status}{last_extra}",
                            )

                            # Save checkpoint after each result for safety
                            save_checkpoint()

                            if verbose:
                                # Show verbose output for each student
                                needs_review = result.needs_review
                                color = "yellow" if needs_review else "green"
                                status = "NEEDS_REVIEW" if needs_review else "OK"
                                mode_color = "cyan" if result.processing_notes == "text" else "magenta"
                                conf_color = "yellow" if result.confidence < settings.review_threshold else "dim"
                                console.print(
                                    f"  [{color}]{result.student_id:12s}[/] {result.student_name:10s} "
                                    f"→ {result.total_score:3.0f}/{result.total_max:3.0f} ({result.pct:3.0f}%) "
                                    f"[{color}]{status}[/] [{mode_color}]{result.processing_notes}[/] "
                                    f"[{conf_color}]conf={result.confidence:.2f}[/]"
                                )
                                # Show uncertain parts inline so rubric issues are visible immediately
                                if result.uncertain_parts:
                                    for up in result.uncertain_parts:
                                        desc = up.description.strip()
                                        console.print(f"    [dim yellow]⚠ {desc}[/]")
                                elif needs_review:
                                    # needs_review but no uncertain_parts → flag why
                                    if result.confidence < settings.review_threshold:
                                        console.print(f"    [dim yellow]⚠ confidence {result.confidence:.2f} < threshold {settings.review_threshold}[/]")
                                    elif result.total_score < result.total_max:
                                        console.print(f"    [dim yellow]⚠ score < max ({result.total_score:.0f}/{result.total_max:.0f})[/]")
                                # Show deductions inline so the user sees why points were lost
                                for b in result.breakdown:
                                    if b.points_awarded < b.points_max:
                                        console.print(f"    [dim red]− {b.criterion} ({b.points_awarded:.0f}/{b.points_max:.0f}): {b.reasoning.strip()}[/]")
                                # Show student-facing feedback (reviewer_notes) for non-perfect scores
                                if result.student_feedback:
                                    console.print(f"    [dim cyan]→ Feedback: {result.student_feedback}[/]")
                        except Exception as e:
                            console.print(f"  [red]Error scoring {sub.student_id}:[/red] {e}")
                            if verbose and hasattr(e, 'raw_response'):
                                console.print(f"  [dim red]--- Full raw response ---[/]")
                                console.print(f"[dim]{e.raw_response}[/]")
                                console.print(f"  [dim red]--- End raw response ---[/]")
                        finally:
                            progress.advance(task)
                finally:
                    # Cancel pending work and shutdown without waiting for blocked workers.
                    # Daemon threads ensure the interpreter won't hang on exit.
                    for f in futures:
                        f.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
        interrupted = True
        if all_results:
            console.print(f"[yellow]Saving {len(all_results)} partial result(s)...[/yellow]")
            save_checkpoint()
            console.print(f"[cyan]Checkpoint saved to {checkpoint_path}[/cyan]")
            _try_auto_submit()
            console.print(f"[dim]Resume later with:[/dim]")
            console.print(f"[bold cyan]{_build_resume_cmd()}[/bold cyan]")
        raise typer.Exit(1)

    if not all_results:
        console.print("[yellow]No results to export.[/yellow]")
        raise typer.Exit(0)

    console.print(f"\n[bold]Step 3/3:[/bold] Exporting {len(all_results)} result(s) → [cyan]{out}[/cyan]")
    export(all_results, out)

    needs_review = sum(1 for r in all_results if r.needs_review)
    console.print(
        f"\n[green]Done.[/green] {needs_review}/{len(all_results)} submission(s) flagged for review "
        f"(highlighted in yellow in the spreadsheet)."
    )
    _try_auto_submit()


@app.command()
def submit(
    course: Annotated[str, typer.Option(help="Blackboard course ID")] = "",
    column: Annotated[str, typer.Option(help="Gradebook column (assignment) ID")] = "",
    scores: Annotated[Path, typer.Option(help="Reviewed Excel spreadsheet")] = Path("scores.xlsx"),
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print what would be submitted without posting")] = False,
    encourage: Annotated[Optional[str], typer.Option("--encourage", "-e", help="Message to attach to perfect scores that have no reviewer notes")] = None,
) -> None:
    """Submit approved scores from the reviewed spreadsheet back to course.pku.edu.cn.

    --course and --column can be omitted if course_id is set in .env and
    assignment_id is present in the spreadsheet (inferred from the first row).
    """
    from auth.iaaa import get_session
    from config import settings
    from review.spreadsheet import load_reviewed
    from submitter.blackboard import submit_scores

    if not scores.exists():
        console.print(f"[red]Error:[/red] Scores file not found: {scores}")
        raise typer.Exit(1)

    records = load_reviewed(scores)

    # Infer column from spreadsheet if not provided
    column_id = column
    if not column_id and records:
        inferred = records[0].result.assignment_id
        if inferred:
            column_id = inferred
            console.print(f"[dim]Inferred column from spreadsheet: {column_id}[/dim]")
    if not column_id:
        console.print("[red]Error:[/red] --column is required (or set assignment_id in the spreadsheet).")
        raise typer.Exit(1)
    # BB REST API needs _423829_1 format; accept bare numeric gradeBookPK too
    col_id = column_id if column_id.startswith("_") else f"_{column_id}_1"

    course_id = course or settings.course_id
    if not course_id:
        console.print("[red]Error:[/red] --course is required (or set COURSE_ID in .env).")
        raise typer.Exit(1)

    approved_count = sum(1 for r in records if r.approved)
    console.print(f"Loaded {len(records)} record(s), {approved_count} approved.")

    if approved_count == 0:
        console.print("[yellow]Nothing to submit — no records marked approved.[/yellow]")
        raise typer.Exit(0)

    console.print("[bold]Authenticating with PKU IAAA…[/bold]")
    client = get_session()

    submit_scores(client, course_id, col_id, records, dry_run=dry_run, encourage=encourage)


@app.command(name="list")
def ls(
    course: Annotated[str, typer.Option(help="Blackboard course ID, e.g. _12345_1")] = "",
    whitelist: Annotated[str, typer.Option(help="Comma-separated student IDs to include; empty = all")] = "",
) -> None:
    """List all assignments with gradeBookPK and submission/grading counts."""
    from auth.iaaa import get_session
    from config import settings
    from crawler.pku_homework import PKUHomeworkCrawler
    from rich.table import Table

    course_id = course or settings.course_id
    if not course_id:
        console.print("[red]Error:[/red] --course is required (or set COURSE_ID in .env)")
        raise typer.Exit(1)

    if whitelist:
        whitelist_ids: set[str] = {s.strip() for s in whitelist.split(",") if s.strip()}
    else:
        student_list_path = Path("student_list")
        if student_list_path.exists():
            whitelist_ids = {line.strip() for line in student_list_path.read_text(encoding="utf-8").splitlines() if line.strip()}
        else:
            whitelist_ids = settings.whitelist_ids

    console.print("[bold]Authenticating with PKU IAAA…[/bold]")
    client = get_session()

    console.print(f"[bold]Fetching assignments for {course_id}…[/bold]")
    crawler = PKUHomeworkCrawler(client, course_id, whitelist_ids)
    assignments = crawler.fetch_assignments()

    if not assignments:
        console.print("[yellow]No assignments found.[/yellow]")
        raise typer.Exit(0)

    table = Table(title=f"Assignments ({len(assignments)})")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Name", style="cyan")
    table.add_column("gradeBookPK (--column)", style="green", no_wrap=True)
    table.add_column("Submitted", justify="right")
    table.add_column("Graded", justify="right")
    table.add_column("Ungraded", justify="right")

    for i, a in enumerate(assignments, 1):
        pk = a.get("gradeBookPK") or a["id"].strip("_").split("_")[0]
        name = a.get("name", "")
        try:
            counts = crawler.count_submissions(pk, name)
            submitted = f"[cyan]{counts['total']}[/cyan]"
            graded = f"[green]{counts['graded']}[/green]" if counts["graded"] else "0"
            ungraded = f"[yellow]{counts['ungraded']}[/yellow]" if counts["ungraded"] else "0"
        except Exception as e:
            submitted = "[red]err[/red]"
            graded = "[red]err[/red]"
            ungraded = "[red]err[/red]"
            console.print(f"  [dim red]Error counting {name}: {e}[/dim red]")
        table.add_row(str(i), name, pk, submitted, graded, ungraded)

    console.print(table)
    console.print("\nUse [bold]--column <gradeBookPK>[/bold] with [bold]ta grade[/bold] or [bold]ta submit[/bold].")


@app.command()
def review(
    scores: Annotated[Path, typer.Option(help="Excel spreadsheet to review")] = Path("scores.xlsx"),
    submissions: Annotated[Path, typer.Option(help="Directory with submission files")] = Path("submissions"),
    rubric: Annotated[Path, typer.Option(help="Path to rubric file to open during review")] = Path("rubric.md"),
    needs_review_only: Annotated[bool, typer.Option("--needs-review", "-n", help="Only review students marked needs_review=YES")] = False,
    all_students: Annotated[bool, typer.Option("--all", "-a", help="Review all students (including already approved)")] = False,
    auto_approve: Annotated[bool, typer.Option("--auto-approve", help="Auto-approve 100-point submissions that don't need review")] = False,
) -> None:
    """Interactive TUI for reviewing submissions one by one.

    Shows score breakdown, opens submission file, and lets you approve or override scores.
    Press 'e' to edit individual criterion scores, 'r' to open the rubric, 'b' to go back.

    Use --auto-approve to automatically approve students with 100/100 and needs_review=NO.
    """
    from review.tui import run_review_tui

    try:
        run_review_tui(
            console=console,
            scores=scores,
            submissions=submissions,
            rubric=rubric,
            needs_review_only=needs_review_only,
            all_students=all_students,
            auto_approve=auto_approve,
        )
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)
    except typer.Exit:
        raise
    except SystemExit:
        raise
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(1)


def _apply_late_penalty(result, submission, due_dt_str: str, console) -> None:
    """Apply 10-point late penalty if submission is ≤7 days late."""
    from datetime import datetime, timezone

    if not due_dt_str or not submission.submitted_at:
        return

    try:
        # Parse due date (ISO 8601 with Z or +00:00)
        due_str = due_dt_str.replace("Z", "+00:00")
        due_dt = datetime.fromisoformat(due_str)
        # Parse submitted_at (local time, treat as UTC for comparison)
        sub_dt = datetime.strptime(submission.submitted_at, "%Y-%m-%d %H:%M:%S")
        sub_dt = sub_dt.replace(tzinfo=timezone.utc)

        delta = sub_dt - due_dt
        late_days = delta.total_seconds() / 86400

        if late_days > 0:
            result.late_days = late_days
            if late_days <= 7:
                penalty = min(10.0, result.total_score)
                result.late_penalty = penalty
                result.total_score -= penalty
                result.breakdown.append(
                    CriterionScore(
                        criterion="晚交扣分",
                        points_awarded=-penalty,
                        points_max=0,
                        reasoning=f"晚交 {late_days:.1f} 天（截止 {submission.submitted_at}，提交 {submission.submitted_at}），扣 10 分",
                    )
                )
                result.needs_review = True
                if result.student_feedback:
                    result.student_feedback += f"\n晚交 {late_days:.1f} 天，扣 10 分。"
                else:
                    result.student_feedback = f"晚交 {late_days:.1f} 天，扣 10 分。"
                console.print(f"  [yellow]{submission.student_id} 晚交 {late_days:.1f} 天，扣 10 分[/yellow]")
            else:
                console.print(f"  [red]{submission.student_id} 晚交超过 7 天（{late_days:.1f} 天），跳过评分[/red]")
                result.total_score = 0
                result.breakdown = [CriterionScore(
                    criterion="晚交超过 7 天",
                    points_awarded=0,
                    points_max=result.total_max,
                    reasoning=f"晚交 {late_days:.1f} 天，超过 7 天限制，不予批改",
                )]
                result.student_feedback = "晚交超过 7 天，不予批改。"
                result.needs_review = True
    except Exception:
        pass


def _save_submissions(submissions: list, save_dir: Path, assignment_title: str) -> int:
    """Save each student's attachment file to save_dir/assignment_title/ for human review.

    Returns the number of files actually written (skips those already present).
    """
    import re
    safe_title = re.sub(r'[^\w\u4e00-\u9fff\-]', '_', assignment_title)
    dest = save_dir / safe_title
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    for sub in submissions:
        for att in sub.attachments:
            ext = Path(att.filename).suffix or ""
            # Filename: studentId_studentName.ext  (e.g. 2300012345_张三.pdf)
            safe_name = re.sub(r'[^\w\u4e00-\u9fff]', '_', sub.student_name)
            filename = f"{sub.student_id}_{safe_name}{ext}"
            target = dest / filename
            if target.exists() and not sub.has_multiple_attempts:
                continue
            target.write_bytes(att.data)
            written += 1
    return written


@app.command()
def batch(
    course: Annotated[str, typer.Option(help="Blackboard course ID, e.g. _12345_1")] = "",
    column: Annotated[str, typer.Option(help="Gradebook column (assignment) ID")] = "",
    rubric: Annotated[Path, typer.Option(help="Path to rubric file")] = Path("rubric.md"),
    batch_size: Annotated[int, typer.Option(help="Number of submissions per batch")] = 5,
    whitelist: Annotated[str, typer.Option(help="Comma-separated student IDs to include; empty = all")] = "",
    out: Annotated[Path, typer.Option(help="Output Excel path")] = Path("scores.xlsx"),
    save_dir: Annotated[Optional[Path], typer.Option(help="Save submission files here for human review; default: submissions/")] = Path("submissions"),
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show intermediate scores for each student")] = False,
    prompt: Annotated[Path, typer.Option(help="System prompt file for the LLM")] = Path("prompts/system_zh.md"),
    threads: Annotated[int, typer.Option(help="Parallel scoring threads")] = 0,
    due_date: Annotated[Optional[str], typer.Option(help="Assignment due date (ISO 8601)")] = None,
    interactive: Annotated[bool, typer.Option("--interactive", "-i", help="Interactively prompt for missing parameters")] = False,
    auto_approve: Annotated[bool, typer.Option("--auto-approve", help="Auto-approve 100-point submissions that don't need review after each batch")] = False,
) -> None:
    """Iterative batch grading: grade a small batch, review/submit, refine rubric, repeat.

    This mode is useful when the rubric is imperfect: grade a batch, discover rubric
    issues during review, fix the rubric, then continue with the next batch using
    the improved rubric. Already-graded batches are preserved and never re-graded.

    Workflow per batch:
      1. Grade N submissions with current rubric
      2. Append results to scores.xlsx
      3. Optionally review and approve
      4. Optionally submit approved scores
      5. Optionally edit rubric before next batch
    """
    from threading import Lock

    from auth.iaaa import get_session
    from config import settings
    from crawler.pku_homework import PKUHomeworkCrawler
    from review.spreadsheet import export_append, load_reviewed
    from review.tui import run_review_tui
    from review.tui_components import prompt_text
    from scorer.llm import score_submission
    from models import ScoringResult
    from submitter.blackboard import submit_scores

    if threads > 0:
        settings.ta_threads = threads

    # Enter interactive mode if explicitly requested or critical params are missing
    course_id = course or settings.course_id
    needs_interactive = interactive or not course_id or not column
    if needs_interactive:
        course_id, column, rubric, whitelist, out, save_dir, prompt, threads, due_date = _interactive_grade_setup(
            course=course,
            column=column,
            rubric=rubric,
            whitelist=whitelist,
            out=out,
            save_dir=save_dir,
            prompt_file=prompt,
            threads=threads,
            due_date=due_date,
            console=console,
        )
        if threads > 0:
            settings.ta_threads = threads

    if not course_id:
        console.print("[red]Error:[/red] --course is required (or set COURSE_ID in .env)")
        raise typer.Exit(1)

    if not rubric.exists():
        console.print(f"[red]Error:[/red] Rubric file not found: {rubric}")
        raise typer.Exit(1)

    rubric_text = rubric.read_text(encoding="utf-8")

    # Determine whitelist
    whitelist_ids: set[str] = (
        {s.strip() for s in whitelist.split(",") if s.strip()}
        if whitelist
        else settings.whitelist_ids
    )

    # Load already-processed student IDs from existing scores.xlsx
    processed_ids: set[str] = set()
    if out.exists():
        try:
            records = load_reviewed(out)
            processed_ids = {r.result.student_id for r in records}
            console.print(f"[bold cyan]Checkpoint:[/bold cyan] {len(processed_ids)} student(s) already processed")
        except Exception as e:
            console.print(f"[yellow]Warning: Could not load existing scores: {e}[/yellow]")

    console.print("[bold]Step 1/3:[/bold] Authenticating with PKU IAAA…")
    client = get_session()

    crawler = PKUHomeworkCrawler(client, course_id, whitelist_ids)

    if column:
        columns = [{"gradeBookPK": column, "name": column, "id": f"_{column}_1"}]
    else:
        console.print("[bold]Step 1b:[/bold] Fetching assignment list…")
        columns = crawler.fetch_assignments()
        console.print(f"  Found {len(columns)} assignment(s).")

    if not columns:
        console.print("[yellow]No assignments found.[/yellow]")
        raise typer.Exit(0)

    # Collect all submissions across all columns (usually just one)
    all_submissions: list = []
    for col in columns:
        grade_book_pk = col.get("gradeBookPK") or col["id"].strip("_").split("_")[0]
        col_title = col.get("name") or col["id"]
        console.print(f"\n[bold]Fetching submissions for [cyan]{col_title}[/cyan]…")

        due_dt_str = due_date or crawler.fetch_due_date(grade_book_pk)
        if due_dt_str:
            console.print(f"  Due date: {due_dt_str}")
        else:
            console.print("  [yellow]Warning: could not determine due date — late penalties disabled[/yellow]")

        submissions = crawler.fetch_submissions(grade_book_pk, col_title, cache_dir=save_dir, verbose=verbose)
        if not submissions:
            console.print("  No submissions found.")
            continue

        # Filter out already graded on website
        already_graded = [s for s in submissions if s.already_graded]
        if already_graded:
            console.print(f"  [dim]Skipping {len(already_graded)} already-graded submission(s)[/dim]")
            submissions = [s for s in submissions if not s.already_graded]

        # Filter out already processed in batch mode
        if processed_ids:
            remaining = [s for s in submissions if s.student_id not in processed_ids]
            skipped = len(submissions) - len(remaining)
            if skipped:
                console.print(f"  [dim]Skipping {skipped} already-processed submission(s)[/dim]")
            submissions = remaining

        if submissions:
            all_submissions.extend(submissions)

    if not all_submissions:
        console.print("[yellow]No remaining submissions to process.[/yellow]")
        raise typer.Exit(0)

    console.print(f"\n[bold green]Total remaining:[/bold green] {len(all_submissions)} submission(s)")
    console.print(f"[dim]Batch size:[/dim] {batch_size}  |  [dim]Threads:[/dim] {settings.ta_threads}")
    console.print(f"[dim]Rubric:[/dim] {rubric}")
    console.print("\n[bold]Starting iterative batch grading…[/bold]")
    console.print("  [dim]After each batch you can review, submit, edit rubric, or continue.[/dim]\n")

    # Process in batches
    batch_num = 0
    total_processed = 0
    start_time = time()

    while all_submissions:
        batch_num += 1
        current_batch = all_submissions[:batch_size]
        all_submissions = all_submissions[batch_size:]
        batch_ids = {s.student_id for s in current_batch}

        console.print(f"\n[bold]═══ Batch {batch_num} ═══[/bold]  ({len(current_batch)} student(s), {len(all_submissions)} remaining)")

        if save_dir:
            written = _save_submissions(current_batch, save_dir, current_batch[0].assignment_title)
            if written:
                console.print(f"  Saved files → [cyan]{save_dir / current_batch[0].assignment_title}[/cyan]")

        # Grade the batch
        batch_results: list[ScoringResult] = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console, transient=not verbose,
        ) as progress:
            task = progress.add_task("  Grading", total=len(current_batch))
            executor = ThreadPoolExecutor(max_workers=settings.ta_threads)
            try:
                futures = {executor.submit(score_submission, sub, rubric_text, prompt): sub for sub in current_batch}
                for future in as_completed(futures):
                    sub = futures[future]
                    try:
                        result = future.result()
                        _apply_late_penalty(result, sub, due_dt_str, console)
                        batch_results.append(result)
                        if verbose:
                            needs_review = result.needs_review
                            color = "yellow" if needs_review else "green"
                            status = "NEEDS_REVIEW" if needs_review else "OK"
                            console.print(
                                f"  [{color}]{result.student_id:12s}[/] {result.student_name:10s} "
                                f"→ {result.total_score:3.0f}/{result.total_max:3.0f} "
                                f"[{color}]{status}[/] conf={result.confidence:.2f}"
                            )
                            if result.uncertain_parts:
                                for up in result.uncertain_parts:
                                    console.print(f"    [dim yellow]⚠ {up.description.strip()}[/]")
                    except Exception as e:
                        console.print(f"  [red]Error scoring {sub.student_id}:[/red] {e}")
                    finally:
                        progress.advance(task)
            finally:
                executor.shutdown(wait=False)

        if not batch_results:
            console.print("[yellow]  No results in this batch.[/yellow]")
            continue

        # Append to scores.xlsx
        export_append(batch_results, out)
        total_processed += len(batch_results)
        processed_ids.update(r.student_id for r in batch_results)

        # Show batch statistics
        avg_score = sum(r.total_score for r in batch_results) / len(batch_results)
        needs_review_count = sum(1 for r in batch_results if r.needs_review)
        avg_confidence = sum(r.confidence for r in batch_results) / len(batch_results)

        console.print(f"\n  [bold]Batch {batch_num} complete:[/bold]")
        console.print(f"    Average score: {avg_score:.1f}")
        console.print(f"    Needs review: {needs_review_count}/{len(batch_results)}")
        console.print(f"    Avg confidence: {avg_confidence:.2f}")

        # Show common uncertain parts (to help discover rubric issues)
        all_uncertain: list[str] = []
        for r in batch_results:
            all_uncertain.extend(up.description for up in r.uncertain_parts)
        if all_uncertain:
            from collections import Counter
            common = Counter(all_uncertain).most_common(3)
            console.print("    [yellow]Common uncertainties:[/yellow]")
            for desc, count in common:
                console.print(f"      • {desc} ({count}x)")

        # Auto-approve if requested
        if auto_approve:
            approved_in_batch = 0
            try:
                records = load_reviewed(out)
                for r in records:
                    if r.result.student_id in batch_ids and not r.approved:
                        if r.result.total_score >= r.result.total_max and not r.result.needs_review:
                            r.approved = True
                            approved_in_batch += 1
                if approved_in_batch:
                    console.print(f"    [green]Auto-approved {approved_in_batch} perfect score(s)[/green]")
            except Exception:
                pass

        # Interactive menu
        while True:
            console.print(f"\n[bold cyan]Batch {batch_num} menu:[/bold cyan]")
            console.print("  [r] review this batch")
            console.print("  [s] submit approved scores")
            console.print("  [n] next batch (continue)")
            console.print("  [e] edit rubric, then continue")
            if not all_submissions:
                console.print("  [q] quit (all done)")
            else:
                console.print("  [q] quit (resume later)")

            choice = prompt_text("Choice", default="n", console=console).strip().lower()

            if choice == "r":
                try:
                    run_review_tui(
                        console=console,
                        scores=out,
                        submissions=save_dir or Path("submissions"),
                        rubric=rubric,
                        all_students=True,
                        filter_ids=batch_ids,
                    )
                except Exception as e:
                    console.print(f"[red]Review error:[/red] {e}")
                # After review, show the menu again
                continue

            elif choice == "s":
                try:
                    records = load_reviewed(out)
                    batch_records = [r for r in records if r.result.student_id in batch_ids and r.approved]
                    if not batch_records:
                        console.print("[yellow]  No approved scores to submit in this batch.[/yellow]")
                        continue
                    col_id = column if column.startswith("_") else f"_{column}_1"
                    submit_scores(client, course_id, col_id, batch_records, dry_run=False)
                    console.print(f"  [green]Submitted {len(batch_records)} score(s).[/green]")
                except Exception as e:
                    console.print(f"[red]Submit error:[/red] {e}")
                # After submit, show the menu again
                continue

            elif choice == "n":
                break

            elif choice == "e":
                console.print(f"\n[bold]Edit rubric file:[/bold] {rubric}")
                console.print("  Modify the file in your editor, then press Enter to continue.")
                prompt_text("Press Enter when done", default="", console=console)
                if rubric.exists():
                    rubric_text = rubric.read_text(encoding="utf-8")
                    console.print("  [green]Rubric reloaded.[/green]")
                else:
                    console.print("  [red]Rubric file not found![/red]")
                break  # Continue to next batch with new rubric

            elif choice == "q":
                console.print(f"\n[bold]Progress saved:[/bold] {total_processed} student(s) in {out}")
                if all_submissions:
                    console.print(f"[dim]{len(all_submissions)} student(s) remaining. Resume with:[/dim]")
                    console.print(f"[bold cyan]uv run python main.py batch --course {course_id} --column {column} --rubric {rubric} --batch-size {batch_size}[/bold cyan]")
                raise typer.Exit(0)

            else:
                console.print("[yellow]  Unknown choice. Try r/s/n/e/q.[/yellow]")

    # All done
    console.print(f"\n[bold green]All batches complete![/bold green] {total_processed} student(s) processed.")
    console.print(f"Results saved to [cyan]{out}[/cyan]")
    console.print("Run [bold]ta review[/bold] to review all scores, then [bold]ta submit[/bold] to upload.")


if __name__ == "__main__":
    app()
