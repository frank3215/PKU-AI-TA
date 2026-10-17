"""
Merge 9 homework averages + midterm from Blackboard CSV export into grade template XLS.

Applies late penalties by crawling Blackboard for submission times and due dates.

Usage:
    uv run python merge_homework_to_template.py <csv_path> <xls_path> [output_path] [--no-bonus]

If output_path is omitted, saves to <xls_path>.replace('.xls', '_filled.xls').
Use --no-bonus to skip bonus application (generates _filled_nobonus.xls by default).

Environment: requires PKU_USERNAME and PKU_PASSWORD in .env for crawling.
"""
from __future__ import annotations

import csv
import math
import os
import re
import sys
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

import xlrd
import xlwt

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).parent))
from auth.iaaa import get_session
from config import settings

BEIJING_TZ = timezone(timedelta(hours=8))
HW_BASE = "/webapps/bb-homeWorkCheck-BBLEARN/homeWorkCheck"

# Column indices in the Blackboard CSV export:
# 0=姓氏, 1=名字, 2=用户名(学号), 4=总计, 5=第一次作业, 6=第二次, 7=第三次,
# 8=第四次, 9=期中成绩, 10=第五次, 11=第六次, 12=第七次, 13=第八次, 14=第九次
HW_COLS = [5, 6, 7, 8, 10, 11, 12, 13, 14]
NUM_HW = len(HW_COLS)
MIDTERM_COL = 9
TOTAL_COL = 4

ASSIGNMENT_NAMES = {
    5: "第一次作业",
    6: "第二次作业",
    7: "第三次作业",
    8: "第四次作业",
    10: "第五次作业",
    11: "第六次作业",
    12: "第七次作业",
    13: "第八次作业",
    14: "第九次作业",
}

MANUAL_DUE = {
    "425320": "2026-04-15 23:59:00",
}

ASSIGNMENT_GBPK = {
    "第一次作业": "423829",
    "第二次作业": "424464",
    "第三次作业": "424850",
    "第四次作业": "425320",
    "第五次作业": "426252",
    "第六次作业": "426963",
    "第七次作业": "427335",
    "第八次作业": "428135",
    "第九次作业": "428577",
}

SCORE_MIN = 0
SCORE_MAX = 100
LATE_CAP = 90
LATE_ZERO_DAYS = 7
MAX_LATE_DAYS_SANE = 200  # sanity cap: anything beyond is probably a data error
STUDENT_ID_PATTERN = re.compile(r"^\d{10}$")


# ── Error types ──────────────────────────────────────────────────────────

class DefensiveError(Exception):
    """A data integrity check failed. Always fatal."""

class DefensiveWarning(UserWarning):
    """A non-fatal anomaly was detected."""


# ── Defensive helpers ────────────────────────────────────────────────────

def _assert(condition: bool, message: str, context: dict | None = None) -> None:
    if not condition:
        parts = [f"[DEFENSIVE FAIL] {message}"]
        if context:
            parts.append(f"  Context: {context}")
        raise DefensiveError("\n".join(parts))


def _warn(message: str, context: dict | None = None) -> None:
    parts = [f"[DEFENSIVE WARN] {message}"]
    if context:
        parts.append(f"  Context: {context}")
    warnings.warn("\n".join(parts))


def _validate_file_exists(path: str, description: str) -> None:
    _assert(os.path.isfile(path), f"{description} not found: {path}")
    size = os.path.getsize(path)
    _assert(size > 0, f"{description} is empty: {path}", {"size": size})


def _validate_student_id(sid: str, context: dict) -> None:
    _assert(bool(sid.strip()), "Student ID is empty", context)
    _assert(STUDENT_ID_PATTERN.match(sid.strip()),
            f"Student ID '{sid}' does not match expected 10-digit format", context)


def _validate_score(val: float | None, label: str, context: dict) -> None:
    if val is None:
        return  # missing is allowed
    _assert(SCORE_MIN <= val <= SCORE_MAX,
            f"{label} score {val} out of range [{SCORE_MIN}, {SCORE_MAX}]", context)


def _validate_late_days(days: float, sid: str, assignment: str) -> None:
    _assert(days >= -1e-9, f"Negative late days ({days:.4f}) for {sid} {assignment}",
            {"sid": sid, "assignment": assignment, "late_days": round(days, 4)})
    _assert(days <= MAX_LATE_DAYS_SANE,
            f"Implausible late days ({days:.1f}) for {sid} {assignment}",
            {"sid": sid, "assignment": assignment, "late_days": round(days, 1)})


def _round_score(val: float) -> float:
    """Round up (ceil) to integer."""
    return math.ceil(val - 1e-9)


# ── Core functions ───────────────────────────────────────────────────────

def parse_ts(ts_str: str) -> datetime | None:
    ts_str = ts_str.strip()
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(BEIJING_TZ)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(ts_str, fmt).replace(tzinfo=BEIJING_TZ)
        except ValueError:
            continue
    return None


def fetch_late_data() -> dict[tuple[str, int], float]:
    """Crawl Blackboard and return {(student_id, col_index): late_days}."""
    from crawler.pku_homework import _parse_student_list, PKUHomeworkCrawler

    print("Authenticating...")
    try:
        cl = get_session()
    except Exception as e:
        raise DefensiveError(f"Authentication failed: {e}")

    course_id = settings.course_id
    _assert(bool(course_id), "COURSE_ID not set in .env")

    crawler = PKUHomeworkCrawler(cl, course_id, set())
    late_map: dict[tuple[str, int], float] = {}
    expected_assignments = set(ASSIGNMENT_NAMES.values())

    for col_idx, asst_name in sorted(ASSIGNMENT_NAMES.items()):
        gbpk = ASSIGNMENT_GBPK[asst_name]
        print(f"\nFetching {asst_name} (gradeBookPK={gbpk})...")

        due_str = MANUAL_DUE.get(gbpk) or crawler.fetch_due_date(gbpk, asst_name)
        due_date = parse_ts(due_str) if due_str else None
        _assert(due_date is not None, f"No due date found for {asst_name}",
                {"gradeBookPK": gbpk})
        print(f"  Due: {due_date.strftime('%Y-%m-%d %H:%M')}")

        try:
            resp = cl.get(
                f"{HW_BASE}/getStudentWork.do",
                params={"course_id": course_id, "gradeBookPK": gbpk, "title": asst_name, "showAll": "true"},
            )
            resp.raise_for_status()
        except Exception as e:
            raise DefensiveError(f"Failed to fetch student list for {asst_name}: {e}")

        students = _parse_student_list(resp.text, verbose=False)
        _assert(len(students) > 0, f"No students returned for {asst_name}",
                {"gradeBookPK": gbpk})
        print(f"  Students: {len(students)}")

        late_count = 0
        for s in students:
            sid = s.get("userId", "")
            sub_str = s.get("submitted_at", "")
            if not sub_str:
                continue
            # Skip students with multiple attempts — they likely had an on-time
            # first attempt and re-submitted late.
            if s.get("has_multiple_attempts", False):
                continue
            sub_dt = parse_ts(sub_str)
            if not sub_dt:
                continue
            if sub_dt <= due_date:
                continue
            late_days = (sub_dt - due_date).total_seconds() / 86400.0
            _validate_late_days(late_days, sid, asst_name)
            late_map[(sid, col_idx)] = late_days
            late_count += 1

        print(f"  Late submissions: {late_count}")

    return late_map


def read_csv_scores(csv_path: str) -> tuple[list[str], list[list[str]]]:
    """Read CSV and return (header, rows)."""
    _validate_file_exists(csv_path, "CSV file")

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)

    _assert(len(header) >= 15, f"CSV header has {len(header)} columns, expected at least 15",
            {"header": header[:10]})
    _assert(len(rows) > 0, "CSV has no data rows")

    # Validate expected column headers
    expected_cols = {
        5: "第一次作业",
        6: "第二次作业",
        7: "第三次作业",
        8: "第四次作业",
        9: "期中成绩",
        10: "第五次作业",
        11: "第六次作业",
        12: "第七次作业",
        13: "第八次作业",
        14: "第九次作业",
    }
    for col_idx, expected_name in expected_cols.items():
        actual = header[col_idx] if col_idx < len(header) else "<MISSING>"
        _assert(expected_name in actual,
                f"CSV column {col_idx} expected to contain '{expected_name}', got '{actual}'",
                {"col": col_idx, "expected": expected_name, "actual": actual})

    return header, rows


def read_template_students(xls_path: str) -> dict[str, tuple[int, str]]:
    """Read XLS template and return {student_id: (row_index, name)}."""
    _validate_file_exists(xls_path, "XLS template")

    try:
        wb = xlrd.open_workbook(xls_path)
    except xlrd.XLRDError as e:
        raise DefensiveError(f"Failed to open XLS file (may not be valid .xls format): {e}")

    _assert(wb.nsheets > 0, "XLS file has no sheets")
    ws = wb.sheet_by_index(0)
    _assert(ws.nrows > 2, f"Template sheet has only {ws.nrows} rows (expected header + data)")

    # Validate header row
    expected_headers = ["XH_学号", "XM_姓名", "XSLB_学生类别", "CJJLFS_成绩记录方式",
                        "PSCJ_平时成绩", "QZCJ_期中成绩", "QMCJ_期末成绩"]
    for i, expected in enumerate(expected_headers):
        actual = str(ws.cell_value(1, i)).strip() if i < ws.ncols else "<MISSING>"
        _assert(expected == actual,
                f"Template header mismatch at col {i}: expected '{expected}', got '{actual}'",
                {"col": i, "expected": expected, "actual": actual})

    students: dict[str, tuple[int, str]] = {}
    duplicate_ids: list[str] = []
    for r in range(2, ws.nrows):
        sid = str(ws.cell_value(r, 0)).strip()
        name = str(ws.cell_value(r, 1)).strip()
        if not sid:
            continue
        _validate_student_id(sid, {"row": r, "name": name})
        if sid in students:
            duplicate_ids.append(sid)
        students[sid] = (r, name)

    if duplicate_ids:
        _warn(f"Duplicate student IDs in template: {duplicate_ids[:5]}",
              {"total_duplicates": len(duplicate_ids)})

    _assert(len(students) > 0, "No students found in template (rows 2+)")
    return students


_NEED_GRADING_WITH_SCORE = re.compile(r'需要评分\((\d+\.?\d*)\)')

def parse_score(val: str) -> float | None:
    val = val.strip()
    if not val or val == "需要评分":
        return None
    # "需要评分(100.00)" — extract the score in parentheses (system has it recorded)
    m = _NEED_GRADING_WITH_SCORE.match(val)
    if m:
        return float(m.group(1))
    try:
        return float(val)
    except ValueError:
        _warn(f"Unparseable score value: '{val}'")
        return None


def merge(csv_path: str, xls_path: str, out_path: str | None = None,
          no_bonus: bool = False) -> str:
    if out_path is None:
        suffix = "_filled_nobonus.xls" if no_bonus else "_filled.xls"
        out_path = xls_path.replace(".xls", suffix)

    # ── Step 1: Crawl late data ──
    print("=" * 60)
    print("Step 1: Crawling late submission data from Blackboard...")
    print("=" * 60)
    try:
        late_map = fetch_late_data()
    except DefensiveError:
        raise
    except Exception as e:
        raise DefensiveError(f"Unexpected error during crawl: {e}")
    print(f"\nTotal late records found: {len(late_map)}")

    # ── Step 2: Read CSV ──
    print("\n" + "=" * 60)
    print("Step 2: Reading CSV scores...")
    print("=" * 60)
    header, rows = read_csv_scores(csv_path)
    print(f"Homework columns:")
    for c in HW_COLS:
        print(f"  HW: col {c} = {header[c]}")
    print(f"  Midterm: col {MIDTERM_COL} = {header[MIDTERM_COL]}")
    print(f"  Total: col {TOTAL_COL} = {header[TOTAL_COL]}")
    print(f"Total students in CSV: {len(rows)}")

    # Validate all student IDs
    csv_sid_set: set[str] = set()
    for row in rows:
        sid = row[2].strip()
        _validate_student_id(sid, {"name": row[1].strip(), "row_preview": row[:5]})
        _assert(sid not in csv_sid_set, f"Duplicate student ID in CSV: {sid}",
                {"name": row[1].strip()})
        csv_sid_set.add(sid)

    # ── Step 3: Apply late penalties with validation ──
    print("\n" + "=" * 60)
    print("Step 3: Applying late penalties...")
    print("=" * 60)
    applied = {"zero": 0, "cap90": 0}
    unchanged_but_late = 0

    for row in rows:
        sid = row[2].strip()
        for c in HW_COLS:
            key = (sid, c)
            late_days = late_map.get(key)
            if late_days is None:
                continue

            score = parse_score(row[c])

            if score is None:
                # Student is late but has no score — anomalous
                _warn(f"{sid} {ASSIGNMENT_NAMES[c]}: late ({late_days:.1f}d) but score is empty",
                      {"late_days": round(late_days, 1)})
                unchanged_but_late += 1
                continue

            _validate_score(score, ASSIGNMENT_NAMES[c],
                            {"sid": sid, "name": row[1].strip()})

            if late_days > LATE_ZERO_DAYS:
                if score > 0:
                    old = score
                    row[c] = "0"
                    applied["zero"] += 1
                    print(f"  {sid}: {ASSIGNMENT_NAMES[c]} {old} → 0 ({late_days:.1f}d > {LATE_ZERO_DAYS}d)")
                else:
                    # Already 0 — verify this is correct
                    unchanged_but_late += 1
            else:
                # ≤7 days late: cap at 90
                if score > LATE_CAP:
                    _assert(score <= SCORE_MAX,
                            f"Score {score} exceeds max {SCORE_MAX}",
                            {"sid": sid, "assignment": ASSIGNMENT_NAMES[c]})
                    row[c] = str(LATE_CAP)
                    applied["cap90"] += 1
                    print(f"  {sid}: {ASSIGNMENT_NAMES[c]} {score} → {LATE_CAP} ({late_days:.1f}d)")
                else:
                    unchanged_but_late += 1

    print(f"  Zeroed (>7 days): {applied['zero']}")
    print(f"  Capped at 90 (≤7 days): {applied['cap90']}")
    if unchanged_but_late:
        print(f"  Late but no change needed: {unchanged_but_late}")

    # ── Step 4: Read template and build student map ──
    print("\n" + "=" * 60)
    print("Step 4: Merging into template...")
    print("=" * 60)
    template = read_template_students(xls_path)
    print(f"Template students: {len(template)}")

    # Build CSV score lookup
    csv_scores: dict[str, dict] = {}
    for row in rows:
        sid = row[2].strip()
        csv_name = row[1].strip()
        hw_scores: list[float | None] = []
        for c in HW_COLS:
            s = parse_score(row[c])
            hw_scores.append(s)
        midterm = parse_score(row[MIDTERM_COL]) if MIDTERM_COL < len(row) else None
        csv_scores[sid] = {"hw": hw_scores, "midterm": midterm, "name": csv_name}

    # Cross-check names between CSV and template for matched students
    name_mismatches = 0
    for sid, (row_idx, tname) in template.items():
        if sid in csv_scores:
            csv_name = csv_scores[sid]["name"]
            if csv_name != tname:
                _warn(f"Name mismatch for {sid}: CSV='{csv_name}' vs Template='{tname}'")
                name_mismatches += 1

    # ── Step 5: Write output ──
    wb = xlrd.open_workbook(xls_path)
    ws = wb.sheet_by_index(0)
    out_wb = xlwt.Workbook(encoding="utf-8")
    out_ws = out_wb.add_sheet("导入成绩模板", cell_overwrite_ok=True)

    EXTRA_COL = 7  # weighted total (not in original template)
    BOOST_COL = 8  # suggested homework to pass (捞分列)
    WEIGHT_HW = 0.2
    WEIGHT_MIDTERM = 0.3
    WEIGHT_FINAL = 0.5
    PASS_THRESHOLD = 60

    for r in range(ws.nrows):
        for c in range(ws.ncols):
            out_ws.write(r, c, ws.cell(r, c).value)

    # Add weighted total header
    out_ws.write(1, EXTRA_COL, "加权总分(20%平时+30%期中+50%期末)")
    out_ws.write(1, BOOST_COL, "建议平时成绩(≥此值及格)")

    matched = 0
    unmatched_template: list[tuple[str, str]] = []
    written_scores: list[float] = []
    written_midterms: list[float] = []
    # Store written values for bonus step (xlwt is write-only, can't read back)
    written_values: dict[str, dict[str, float | None]] = {}

    for sid, (row_idx, tname) in template.items():
        if sid in csv_scores:
            s = csv_scores[sid]
            wv: dict[str, float | None] = {"hw": None, "mid": None}

            # Homework average → PSCJ (col 4)
            valid = [sc for sc in s["hw"] if sc is not None]
            hw_avg: float | None = None
            if valid:
                _assert(len(valid) <= NUM_HW,
                        f"Student {sid} has {len(valid)} valid scores, expected ≤{NUM_HW}")
                avg = sum(valid) / NUM_HW
                _assert(SCORE_MIN <= avg <= SCORE_MAX,
                        f"Average score {avg} out of range for {sid}",
                        {"valid_scores": valid, "avg": round(avg, 4)})
                hw_avg = _round_score(avg)
                out_ws.write(row_idx, 4, hw_avg)
                written_scores.append(hw_avg)
                wv["hw"] = hw_avg

            # Midterm → QZCJ (col 5)
            if s["midterm"] is not None:
                _validate_score(s["midterm"], "Midterm", {"sid": sid, "name": tname})
                out_ws.write(row_idx, 5, s["midterm"])
                written_midterms.append(s["midterm"])
                wv["mid"] = s["midterm"]

            written_values[sid] = wv

            # Weighted total → EXTRA_COL (col 7): Excel formula =E*0.2+F*0.3+G*0.5
            excel_row = row_idx + 1
            out_ws.write(row_idx, EXTRA_COL, xlwt.Formula(
                f"E{excel_row}*0.2+F{excel_row}*0.3+G{excel_row}*0.5"
            ))

            # 捞分列 → BOOST_COL (col 8): suggested homework avg to pass
            # Shows the minimum homework average needed to reach 60 when weighted < 60
            out_ws.write(row_idx, BOOST_COL, xlwt.Formula(
                f'IF(AND(H{excel_row}>0,H{excel_row}<60),'
                f'ROUNDUP(MAX(0,(60-F{excel_row}*0.3-G{excel_row}*0.5)/0.2),0),"")'
            ))

            matched += 1
        else:
            unmatched_template.append((sid, tname))

    unmatched_csv = [sid for sid in csv_scores if sid not in template]

    # ── Step 5: Apply bonus from bonus file (unless --no-bonus) ──
    if not no_bonus:
        bonus_path = Path(__file__).parent / "25-26-2算法设计与分析（1）班成绩（加分）.xls"
        if bonus_path.exists():
            print("\n" + "=" * 60)
            print("Step 5: Applying bonuses from bonus file...")
            print("=" * 60)
            WEIGHT_HW = 0.2
            WEIGHT_MIDTERM = 0.3

            bonus_wb = xlrd.open_workbook(str(bonus_path))
            bonus_ws = bonus_wb.sheet_by_index(0)
            # Header: col 7=校内赛加分, col 8=课堂回答加分, col 9=总加分
            bonus_data: dict[str, float] = {}
            for r in range(2, bonus_ws.nrows):
                sid = str(bonus_ws.cell_value(r, 0)).strip()
                total = bonus_ws.cell_value(r, 9) if bonus_ws.ncols > 9 else 0
                if isinstance(total, (int, float)) and total > 0:
                    bonus_data[sid] = total

            print(f"  Students with bonus: {len(bonus_data)}")

            bonus_applied_hw = 0
            bonus_applied_mid = 0
            bonus_overflow_hw = 0
            bonus_overflow_mid = 0
            for sid, (row_idx, tname) in template.items():
                total_bonus = bonus_data.get(sid)
                if total_bonus is None:
                    continue

                wv = written_values.get(sid)
                if wv is None:
                    continue

                current_hw = wv.get("hw")
                current_mid = wv.get("mid")
                current_hw = float(current_hw) if current_hw is not None else 0.0
                current_mid = float(current_mid) if current_mid is not None else 0.0

                # Strategy: add to 平时 first (weight 0.2), then 期中 (weight 0.3)
                remaining = total_bonus

                # How much can we add to 平时?
                max_hw_add = max(0, SCORE_MAX - current_hw)
                hw_needed = remaining / WEIGHT_HW if WEIGHT_HW > 0 else 0
                bonus_hw = min(hw_needed, max_hw_add)
                remaining -= bonus_hw * WEIGHT_HW
                if bonus_hw > 0:
                    new_hw = _round_score(current_hw + bonus_hw)
                    out_ws.write(row_idx, 4, new_hw)
                    wv["hw"] = new_hw
                    bonus_applied_hw += 1
                    if abs(bonus_hw - hw_needed) > 0.001:
                        bonus_overflow_hw += 1

                # Put remainder into 期中
                if remaining > 0.001:
                    max_mid_add = max(0, SCORE_MAX - current_mid)
                    mid_needed = remaining / WEIGHT_MIDTERM if WEIGHT_MIDTERM > 0 else 0
                    bonus_mid = min(mid_needed, max_mid_add)
                    remaining -= bonus_mid * WEIGHT_MIDTERM
                    if bonus_mid > 0:
                        new_mid = _round_score(current_mid + bonus_mid)
                        out_ws.write(row_idx, 5, new_mid)
                        wv["mid"] = new_mid
                        bonus_applied_mid += 1
                        if abs(bonus_mid - mid_needed) > 0.001:
                            bonus_overflow_mid += 1

                # If still remaining after both caps, log warning
                if remaining > 0.001:
                    _warn(f"Bonus {total_bonus} for {sid} {tname}: could only distribute {total_bonus - remaining:.2f}, "
                          f"{remaining:.2f} remaining (hw={current_hw}, mid={current_mid})")

            if bonus_applied_hw:
                print(f"  Bonuses applied to 平时成绩: {bonus_applied_hw}")
            if bonus_applied_mid:
                print(f"  Bonuses applied to 期中成绩: {bonus_applied_mid}")
            if bonus_overflow_hw:
                print(f"  Capped at 100 in 平时成绩: {bonus_overflow_hw}")
            if bonus_overflow_mid:
                print(f"  Capped at 100 in 期中成绩: {bonus_overflow_mid}")

        else:
            print(f"\n  [INFO] Bonus file not found at {bonus_path}, skipping")
    else:
        print("\n" + "=" * 60)
        print("Step 5: Skipping bonus (--no-bonus flag set)")
        print("=" * 60)

    # ── Post-write validation ──
    if written_scores:
        _assert(min(written_scores) >= SCORE_MIN,
                f"Minimum written average {min(written_scores)} < {SCORE_MIN}")
        _assert(max(written_scores) <= SCORE_MAX,
                f"Maximum written average {max(written_scores)} > {SCORE_MAX}")
    if written_midterms:
        _assert(min(written_midterms) >= SCORE_MIN,
                f"Minimum written midterm {min(written_midterms)} < {SCORE_MIN}")
        _assert(max(written_midterms) <= SCORE_MAX,
                f"Maximum written midterm {max(written_midterms)} > {SCORE_MAX}")

    # Write and verify
    try:
        out_wb.save(out_path)
    except Exception as e:
        raise DefensiveError(f"Failed to save output file: {e}")

    _validate_file_exists(out_path, "Output file")

    # ── Summary ──
    print(f"\n  Matched: {matched} students")
    if name_mismatches:
        print(f"  [WARN] Name mismatches between CSV and template: {name_mismatches}")
    if unmatched_csv:
        print(f"  Unmatched in CSV (not in template): {len(unmatched_csv)}")
        for sid in sorted(unmatched_csv)[:5]:
            print(f"    {sid} {csv_scores[sid]['name']}")
    if unmatched_template:
        print(f"  Unmatched in template (not in CSV): {len(unmatched_template)}")
        for sid, name in unmatched_template[:5]:
            print(f"    {sid} {name}")

    print(f"\nSaved to: {out_path}")
    return out_path


def main():
    if len(sys.argv) < 3:
        print(__doc__.strip())
        sys.exit(1)

    csv_path = sys.argv[1]
    xls_path = sys.argv[2]
    out_path = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("--") else None
    no_bonus = "--no-bonus" in sys.argv

    try:
        merge(csv_path, xls_path, out_path, no_bonus=no_bonus)
    except DefensiveError as e:
        print(f"\n[ERROR] {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"\n[UNEXPECTED ERROR] {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
