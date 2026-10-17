"""
Only crawl Blackboard for late submission analysis.
No local data is read. Uses _BBClient directly (not httpx).
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table

sys.path.insert(0, str(Path(__file__).parent))

from auth.iaaa import get_session
from crawler.pku_homework import _parse_student_list, HW_BASE
from config import settings

console = Console()

BEIJING_TZ = timezone(timedelta(hours=8))

_DUE_DATE_RE = re.compile(
    r"(?:截至时间|截止时间|Due)[：:\s]*</span>\s*<span[^>]*>([\d\-:\s]+)</span>",
    re.DOTALL,
)

_INPUTDATA_RE = re.compile(
    r'<input[^>]*(?:name=["\'](?:inputData|grade)["\'][^>]*value=["\']([^"\']*)["\']'
    r'|value=["\']([^"\']*)["\'][^>]*name=["\'](?:inputData|grade)["\'])',
    re.IGNORECASE,
)

_STUDENT_META_RE = re.compile(
    r'<a[^>]*(?:CheckAloneWork|CheckWork)\.do\?course_id=[^&]+&gradeBookPK=(\d+)'
    r'&userId=(\d+)&filePk=(\d+)&title=([^&"]+)&attemptPk=(\d+)[^>]*>([^<]+)</a>'
)


def parse_ts(ts_str: str) -> datetime | None:
    ts_str = ts_str.strip()
    # Try ISO 8601 first (e.g., "2026-06-20T15:59:00.000Z")
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(BEIJING_TZ)
    except ValueError:
        pass
    # Try common Chinese formats (no tz = Beijing time)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts_str, fmt).replace(tzinfo=BEIJING_TZ)
        except ValueError:
            continue
    return None


def main():
    console.print("[bold]Authenticating...[/bold]")
    cl = get_session()  # _BBClient

    course_id = settings.course_id
    if not course_id:
        console.print("[red]COURSE_ID not set in .env[/red]")
        return

    # ── Fetch assignments ──
    console.print("[bold]Fetching assignment list...[/bold]")
    from crawler.pku_homework import PKUHomeworkCrawler
    crawler = PKUHomeworkCrawler(cl, course_id, set())
    assignments = crawler.fetch_assignments()
    console.print(f"Found {len(assignments)} assignment(s)\n")

    all_late: list[dict] = []

    for a in assignments:
        gbpk = a["gradeBookPK"]
        title = a.get("name", a.get("title", ""))
        console.print(f"[cyan]── {title} (ID: {gbpk}) ──[/cyan]")

        # Due date via crawler (tries BB REST API then HTML fallback)
        # Manual overrides for assignments without Blackboard due date
        manual_due = {
            "425320": "2026-04-15 23:59:00",  # 第四次作业，用户指定截止日期4月15日
        }
        due_str = manual_due.get(gbpk) or crawler.fetch_due_date(gbpk, title)
        due_date = parse_ts(due_str) if due_str else None

        if due_date:
            console.print(f"  Due: {due_date.strftime('%Y-%m-%d %H:%M')}")
        else:
            console.print(f"  [yellow]No due date found[/yellow]")

        # Fetch student list
        resp = cl.get(
            f"{HW_BASE}/getStudentWork.do",
            params={"course_id": course_id, "gradeBookPK": gbpk, "title": title, "showAll": "true"},
        )
        resp.raise_for_status()
        html = resp.text

        # Student list
        students = _parse_student_list(html, verbose=False)
        console.print(f"  Students: {len(students)}")

        # Pre-extract student meta (filePk, attemptPk, already_graded) for grade fetching
        student_meta = {}
        for sm in _STUDENT_META_RE.finditer(html):
            uid = sm.group(2)
            student_meta[uid] = {
                "filePk": sm.group(3),
                "attemptPk": sm.group(5),
                "already_graded": sm.group(6) == "查看",
            }

        assignment_late = 0
        for s in students:
            sid = s.get("userId", "")
            name = s.get("studentName", s.get("name", ""))
            sub_str = s.get("submitted_at", "")

            if not due_date or not sub_str:
                continue

            sub_dt = parse_ts(sub_str)
            if not sub_dt or sub_dt <= due_date:
                continue

            late_days = (sub_dt - due_date).total_seconds() / 86400.0
            assignment_late += 1

            # Fetch existing online grade
            existing_grade = None
            meta = student_meta.get(sid)
            if meta:
                endpoint = f"{HW_BASE}/CheckAloneWork.do" if meta["already_graded"] else f"{HW_BASE}/CheckWork.do"
                try:
                    gr = cl.get(endpoint, params={
                        "course_id": course_id, "gradeBookPK": gbpk,
                        "userId": sid, "filePk": meta["filePk"],
                        "title": title, "attemptPk": meta["attemptPk"],
                    })
                    gm = _INPUTDATA_RE.search(gr.text)
                    if gm:
                        existing_grade = gm.group(1) or gm.group(2)
                except Exception:
                    pass

            info = {
                "name": name, "sid": sid, "assignment": title,
                "submitted_at": sub_str, "late_days": round(late_days, 2),
                "grade": existing_grade,
            }
            all_late.append(info)

            status = ""
            if existing_grade:
                score = float(existing_grade)
                if late_days > 7 and score > 0:
                    status = " ⚠ >7天仍有分!"
                if score >= 90:
                    status += " 🔥 ≥90!"
            else:
                status = " (无在线分数)"

            console.print(f"    {name}: 晚交{late_days:.2f}天, 在线成绩={existing_grade or 'N/A'}{status}")

        if assignment_late == 0:
            console.print(f"  [green]No late submissions[/green]")
        console.print()

    # ── Summary ──
    console.print("\n" + "=" * 70)
    console.print("[bold]SUMMARY[/bold]")
    console.print("=" * 70)

    # A: Late >7 days with points
    console.print("\n[bold red]A. 晚交超过7天但仍有分数[/bold red]")
    tbl_a = Table()
    tbl_a.add_column("学生", style="cyan")
    tbl_a.add_column("学号")
    tbl_a.add_column("作业", style="yellow")
    tbl_a.add_column("晚交天数", justify="right")
    tbl_a.add_column("在线分数", justify="right")
    found_a = False
    for r in all_late:
        if r["late_days"] > 7 and r["grade"] and float(r["grade"]) > 0:
            found_a = True
            tbl_a.add_row(r["name"], r["sid"], r["assignment"],
                          str(r["late_days"]), r["grade"])
    console.print(tbl_a if found_a else "  [green]None found[/green]")

    # B: Late with score >= 90
    console.print("\n[bold yellow]B. 晚交作业分数≥90[/bold yellow]")
    tbl_b = Table()
    tbl_b.add_column("学生", style="cyan")
    tbl_b.add_column("学号")
    tbl_b.add_column("作业", style="yellow")
    tbl_b.add_column("晚交天数", justify="right")
    tbl_b.add_column("在线分数", justify="right")
    found_b = False
    for r in all_late:
        if r["grade"] and float(r["grade"]) >= 90:
            found_b = True
            tbl_b.add_row(r["name"], r["sid"], r["assignment"],
                          str(r["late_days"]), r["grade"])
    console.print(tbl_b if found_b else "  [green]None found[/green]")

    # C: All late
    console.print("\n[bold]C. 所有晚交汇总[/bold]")
    tbl_c = Table()
    tbl_c.add_column("学生", style="cyan")
    tbl_c.add_column("学号")
    tbl_c.add_column("作业", style="yellow")
    tbl_c.add_column("晚交天数", justify="right")
    tbl_c.add_column("在线分数", justify="right")
    tbl_c.add_column("状态")
    for r in all_late:
        status = "[green]正常[/green]"
        if r["late_days"] > 7 and r["grade"] and float(r["grade"]) > 0:
            status = "[red]异常:>7天未扣分[/red]"
        elif r["grade"] and float(r["grade"]) >= 90:
            status = "[yellow]晚交高分[/yellow]"
        tbl_c.add_row(r["name"], r["sid"], r["assignment"],
                      str(r["late_days"]), r["grade"] or "N/A", status)
    console.print(tbl_c)
    console.print(f"\n总计 {len(all_late)} 条晚交记录")


if __name__ == "__main__":
    main()
