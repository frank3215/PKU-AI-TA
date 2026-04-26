"""
Submit grades back to the PKU homework plugin (bb-homeWorkCheck-BBLEARN).

Endpoint discovered by inspecting the CheckWork.do page JS sendData() function:
  POST saveStudentGrade.do
  Body (form-encoded):
    inputData   — numeric score
    attemptPk   — per-student attempt identifier
    gradeBookPk — assignment identifier (note lowercase 'k')
    course_id   — course identifier
    richContent — reviewer notes
    gradePk     — per-student grade record ID (hardcoded in sendData() JS)

gradePk is injected server-side into the page JS for each student, so we must
fetch CheckWork.do per student to extract it.
"""
from __future__ import annotations

import re
from typing import Optional

import httpx
from rich.console import Console
from rich.prompt import Confirm
from rich.table import Table

from models import ReviewRecord

HW_BASE = "/webapps/bb-homeWorkCheck-BBLEARN/homeWorkCheck"
SUBMIT_ENDPOINT = f"{HW_BASE}/saveStudentGrade.do"

console = Console()

_GRADE_PK_RE = re.compile(r"gradePk=[^)]*encodeURIComponent\((\d+)\)")
# Extract existing grade score from CheckWork.do / CheckAloneWork.do HTML.
# The input element may have name="inputData" (older) or name="grade" (newer).
_INPUTDATA_RE = re.compile(
    r'<input[^>]*(?:name=["\'](?:inputData|grade)["\'][^>]*value=["\']([^"\']*)["\']|value=["\']([^"\']*)["\'][^>]*name=["\'](?:inputData|grade)["\'])',
    re.IGNORECASE,
)


def _fetch_assignment_title(client: httpx.Client, course_id: str, grade_book_pk: str) -> str:
    """Look up the assignment title for a gradeBookPK via getHomeWorkList.do."""
    from crawler.pku_homework import _parse_homework_list

    resp = client.get(
        f"{HW_BASE}/getHomeWorkList.do",
        params={"course_id": course_id},
    )
    resp.raise_for_status()
    assignments = _parse_homework_list(resp.text)
    for a in assignments:
        if a.get("gradeBookPK") == grade_book_pk:
            return a.get("name", "")
    return ""


def _fetch_student_meta(client: httpx.Client, course_id: str, grade_book_pk: str) -> tuple[dict[str, dict], str]:
    """
    Return ({userId: {filePk, attemptPk, already_graded}}, assignment_title) by parsing getStudentWork.do.

    already_graded is True when the link text is "查看" (view = already graded),
    False when it is "批改" (grade = needs grading).
    """
    from crawler.pku_homework import _STUDENT_ONCLICK_PATTERN, _STUDENT_PATTERN

    title = _fetch_assignment_title(client, course_id, grade_book_pk)

    resp = client.get(
        f"{HW_BASE}/getStudentWork.do",
        params={"course_id": course_id, "gradeBookPK": grade_book_pk, "title": title, "showAll": "true"},
    )
    resp.raise_for_status()
    html = resp.text

    meta: dict[str, dict] = {}
    for m in _STUDENT_PATTERN.finditer(html):
        groups = m.groups()
        if len(groups) == 6:
            _, user_id, file_pk, _, attempt_pk, link_text = groups
        else:
            _, user_id, file_pk, _, attempt_pk = groups
            link_text = ""
        already_graded = link_text.strip() == "查看"
        meta[user_id] = {"filePk": file_pk, "attemptPk": attempt_pk, "already_graded": already_graded}
    for m in _STUDENT_ONCLICK_PATTERN.finditer(html):
        groups = m.groups()
        if len(groups) == 4:
            user_id, file_pk, attempt_pk, link_text = groups
        else:
            user_id, file_pk, attempt_pk = groups
            link_text = ""
        already_graded = link_text.strip() == "查看"
        meta.setdefault(user_id, {"filePk": file_pk, "attemptPk": attempt_pk, "already_graded": already_graded})

    return meta, title


def _fetch_grade_pk(
    client: httpx.Client,
    course_id: str,
    grade_book_pk: str,
    user_id: str,
    file_pk: str,
    attempt_pk: str,
    title: str,
) -> str | None:
    """
    Fetch CheckWork.do for one student and extract gradePk from sendData() JS.
    gradePk is a per-student grade record ID injected server-side.
    """
    resp = client.get(
        f"{HW_BASE}/CheckWork.do",
        params={
            "course_id": course_id,
            "gradeBookPK": grade_book_pk,
            "userId": user_id,
            "filePk": file_pk,
            "title": title,
            "attemptPk": attempt_pk,
        },
    )
    resp.raise_for_status()
    html = resp.text

    m = _GRADE_PK_RE.search(html)
    return m.group(1) if m else None


def _fetch_existing_grade(
    client: httpx.Client,
    course_id: str,
    grade_book_pk: str,
    user_id: str,
    file_pk: str,
    attempt_pk: str,
    title: str,
    already_graded: bool = False,
) -> str | None:
    """Fetch CheckWork.do (or CheckAloneWork.do) for one student and extract the existing score."""
    endpoint = f"{HW_BASE}/CheckAloneWork.do" if already_graded else f"{HW_BASE}/CheckWork.do"
    resp = client.get(
        endpoint,
        params={
            "course_id": course_id,
            "gradeBookPK": grade_book_pk,
            "userId": user_id,
            "filePk": file_pk,
            "title": title,
            "attemptPk": attempt_pk,
        },
    )
    resp.raise_for_status()
    m = _INPUTDATA_RE.search(resp.text)
    if m:
        return m.group(1) or m.group(2)
    return None


def submit_scores(
    client: httpx.Client,
    course_id: str,
    column_id: str,
    records: list[ReviewRecord],
    *,
    dry_run: bool = False,
    encourage: Optional[str] = None,
) -> None:
    """
    Submit approved grades via saveStudentGrade.do.

    column_id may be "_423829_1" (BB REST format) or bare "423829" (gradeBookPK).
    """
    grade_book_pk = column_id.strip("_").split("_")[0]

    approved = [r for r in records if r.approved]
    skipped = len(records) - len(approved)
    if skipped:
        console.print(f"[yellow]Skipping {skipped} unapproved record(s).[/yellow]")
    if not approved:
        return

    # ── Step 1: fetch filePk / attemptPk for all students ────────────────
    console.print("  Fetching submission metadata…")
    try:
        student_meta, assignment_title = _fetch_student_meta(client, course_id, grade_book_pk)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Error fetching student list:[/red] {e}")
        return
    console.print(f"  Found metadata for {len(student_meta)} student(s). Assignment: [cyan]{assignment_title}[/cyan]")

    # ── Step 2: fetch existing online grades and build diff table ─────────
    console.print("  Fetching existing online grades…")
    diff_rows: list[tuple[ReviewRecord, str | None]] = []
    for r in approved:
        uid = r.result.student_id
        if uid not in student_meta:
            continue
        meta = student_meta[uid]
        try:
            existing = _fetch_existing_grade(
                client, course_id, grade_book_pk,
                uid, meta["filePk"], meta["attemptPk"], assignment_title,
                already_graded=meta.get("already_graded", False),
            )
        except httpx.HTTPStatusError:
            existing = None
        diff_rows.append((r, existing))

    if not diff_rows:
        console.print("[yellow]No students found online to compare.[/yellow]")
        return

    # Show diff table
    console.print()
    diff_table = Table(title="Grade Diff (Local vs Online)")
    diff_table.add_column("Student ID", style="cyan", no_wrap=True)
    diff_table.add_column("Name", style="cyan")
    diff_table.add_column("Local", justify="right")
    diff_table.add_column("Online", justify="right")
    diff_table.add_column("Status", justify="center")

    for r, existing in diff_rows:
        local_score = f"{r.final_score:.0f}"
        online_score = existing if existing else "—"
        if existing is None:
            status = "[dim]new[/dim]"
        elif float(existing) == r.final_score:
            status = "[green]same[/green]"
        else:
            status = f"[red]CHANGED {float(existing):.0f} → {r.final_score:.0f}[/red]"
        diff_table.add_row(
            r.result.student_id,
            r.result.student_name,
            local_score,
            online_score,
            status,
        )
    console.print(diff_table)
    console.print()

    if dry_run:
        for r, existing in diff_rows:
            notes_display = r.reviewer_notes or ""
            if encourage and r.final_score >= r.result.total_max and not notes_display.strip():
                notes_display = encourage
            console.print(
                f"[dim][DRY RUN][/dim] Would submit: "
                f"{r.result.student_id} ({r.result.student_name})"
                f" → {r.final_score}/{r.result.total_max}"
                f"  notes: {notes_display[:60]}"
            )
        return

    if not Confirm.ask("[bold cyan]Proceed with upload?[/bold cyan]", default=False):
        console.print("[yellow]Upload cancelled.[/yellow]")
        return

    # ── Step 3: upload ───────────────────────────────────────────────────
    ok = 0
    for r in approved:
        uid = r.result.student_id
        score = r.final_score
        notes = (r.reviewer_notes or "").strip()

        if uid not in student_meta:
            console.print(f"[yellow]⚠[/yellow]  {uid} ({r.result.student_name}): no submission metadata — skipping")
            continue

        meta = student_meta[uid]

        # Fetch CheckWork.do to get gradePk
        try:
            grade_pk = _fetch_grade_pk(
                client, course_id, grade_book_pk,
                uid, meta["filePk"], meta["attemptPk"], assignment_title,
            )
        except httpx.HTTPStatusError as e:
            console.print(f"[red]✗[/red]  {uid} ({r.result.student_name}): "
                          f"failed to load CheckWork.do — {e}")
            continue

        if grade_pk is None:
            console.print(f"[yellow]⚠[/yellow]  {uid} ({r.result.student_name}): "
                          "gradePk not found in page JS — skipping")
            continue

        score_str = str(int(score)) if score == int(score) else str(score)

        # Attach encouraging message for perfect scores with no notes
        final_notes = notes
        if encourage and score >= r.result.total_max and not notes.strip():
            final_notes = encourage

        payload = {
            "inputData": score_str,
            "attemptPk": meta["attemptPk"],
            "gradeBookPk": grade_book_pk,
            "course_id": course_id,
            "richContent": final_notes[:2000],
            "gradePk": grade_pk,
        }

        try:
            resp = client.post(SUBMIT_ENDPOINT, data=payload)
            resp.raise_for_status()
            console.print(
                f"[green]✓[/green]  {uid} ({r.result.student_name})"
                f" → {score_str}/{r.result.total_max}"
            )
            ok += 1
        except httpx.HTTPStatusError as e:
            console.print(
                f"[red]✗[/red]  {uid} ({r.result.student_name}): "
                f"HTTP {e.response.status_code} — {e.response.text[:300]}"
            )

    result_color = "green" if ok == len(approved) else "yellow"
    console.print(
        f"\n[{result_color}]Done.[/{result_color}] {ok}/{len(approved)} grade(s) submitted."
    )
