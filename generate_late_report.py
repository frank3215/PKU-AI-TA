"""
Generate a comprehensive Markdown report of all late submission penalties
and reconciliation of Chen GeFei's CSV jiaoyan values.

Usage:
    uv run python generate_late_report.py <raw_csv_path> [output_path] [--download]

The raw CSV path is the UTF-16 LE TSV from Blackboard (13-53-35).
Output: a Markdown file with late penalties + jiaoyan reconciliation.
--download: also download all multi-attempt cross-zone submission files.
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
import sys
import warnings
from datetime import datetime, timezone, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")

sys.path.insert(0, str(Path(__file__).parent))
from auth.iaaa import get_session
from config import settings
from crawler.pku_homework import _parse_student_list, PKUHomeworkCrawler

BEIJING_TZ = timezone(timedelta(hours=8))
HW_BASE = "/webapps/bb-homeWorkCheck-BBLEARN/homeWorkCheck"

HW_COLS = [5, 6, 7, 8, 10, 11, 12, 13, 14]
NUM_HW = len(HW_COLS)
MIDTERM_COL = 9

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

MANUAL_DUE = {
    "425320": "2026-04-15 23:59:00",
}

LATE_ZERO_DAYS = 7
LATE_CAP = 90
SCORE_MAX = 100

HERE = Path(__file__).parent
CHENGEFEI_CSV = HERE / "副本2026算分期末总评（陈格非）7-15（已加分处理）2.csv"
BONUS_XLS = HERE / "25-26-2算法设计与分析（1）班成绩（加分）.xls"
FILLED_XLS = HERE / "副本25-26-2算法设计与分析（1）班成绩模板1_filled.xls"


def _round_score(val: float) -> float:
    return math.ceil(val - 1e-9)


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


_NEED_GRADING_RE = re.compile(r'需要评分\((\d+\.?\d*)\)')

def parse_score(val: str) -> float | None:
    val = val.strip()
    if not val:
        return None
    if val == "需要评分":
        return 0.0  # ungraded → 0
    # "需要评分(100.00)" → extract score from parentheses
    m = _NEED_GRADING_RE.match(val)
    if m:
        return float(m.group(1))
    try:
        return float(val)
    except ValueError:
        return None


# Per-row HTML parser: extract all attempts with their submission times
_TR_RE = re.compile(r'<tr[^>]*id="listContainer_row:(\d+)"[^>]*>(.*?)</tr>', re.DOTALL)
_SID_IN_TR = re.compile(r'<th[^>]*>\s*(\d{10})\s*</th>')
_NAME_IN_TR = re.compile(r'<span class="table-data-cell-value">(.+?)</span>')
_TIME_IN_TR = re.compile(r'提交时间[:\s]*</span>\s*<span[^>]*>([\d\-:\s]+)</span>')
_ATTEMPT_INFO = re.compile(
    r'(CheckAloneWork|CheckWork)\.do\?[^"]*?'
    r'userId=(\d+).*?'
    r'filePk=(\d+).*?'
    r'attemptPk=(\d+)'
)
_GRADED_STATUS = re.compile(
    r'(?:CheckAloneWork|CheckWork)\.do[^>]*>([^<]+)</a>'
)
_ONCLICK_ATTEMPT_TR = re.compile(
    r"""checkWork\(\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'\s*"""
)
_ONCLICK_GRADED = re.compile(
    r"""onclick=["'][^"']*checkWork[^>]*>([^<]+)</a>"""
)


def _parse_per_row_attempts(html: str, due_date: datetime) -> dict[str, list[dict]]:
    """Extract ALL attempts per student from getStudentWork.do HTML rows.

    Unlike _parse_student_list which keeps only the newest attempt,
    this returns every attempt with its own submission time, zone, and graded status.
    """
    attempts: dict[str, list[dict]] = {}
    all_rows = _TR_RE.findall(html)
    for tr_id, tr_content in all_rows:
        sid_m = _SID_IN_TR.search(tr_content)
        if not sid_m:
            continue
        sid = sid_m.group(1)
        name_m = _NAME_IN_TR.search(tr_content)
        name = name_m.group(1).strip() if name_m else "?"
        time_m = _TIME_IN_TR.search(tr_content)
        sub_time_str = time_m.group(1).strip() if time_m else ""
        sub_dt = parse_ts(sub_time_str) if sub_time_str else None
        apk_m = _ATTEMPT_INFO.search(tr_content)
        if apk_m:
            link_type = apk_m.group(1)  # CheckAloneWork or CheckWork
            apk = apk_m.group(4)  # attemptPk is group 4
            file_pk = apk_m.group(3)  # filePk is group 3
            graded_label = "已批" if link_type == "CheckAloneWork" else "未批"
        else:
            oc_m = _ONCLICK_ATTEMPT_TR.search(tr_content)
            if oc_m:
                apk = oc_m.group(3)
                file_pk = oc_m.group(2)
                graded_label = "未批"  # onclick is always CheckWork
            else:
                apk = "?"
                file_pk = "?"
                # Graded status: "查看" = already graded, "批改" = needs grading
                graded_m = _GRADED_STATUS.search(tr_content)
                graded = graded_m.group(1).strip() if graded_m else "?"
                graded_label = "已批" if graded == "查看" else ("未批" if graded == "批改" else graded)
        # Zone classification
        if sub_dt and due_date:
            if sub_dt <= due_date:
                zone = "A-按时"
            else:
                late_days = (sub_dt - due_date).total_seconds() / 86400.0
                zone = "B-≤7d上限90" if late_days <= LATE_ZERO_DAYS else "C->7d零分"
        else:
            zone = "?"
        if sid not in attempts:
            attempts[sid] = []
        attempts[sid].append({
            "attemptPk": apk,
            "filePk": file_pk,
            "name": name,
            "sub_time_str": sub_time_str,
            "sub_dt": sub_dt,
            "zone": zone,
            "graded": graded_label,
        })
    # Sort each student's attempts by attemptPk
    for sid in attempts:
        attempts[sid].sort(key=lambda x: int(x["attemptPk"]) if x["attemptPk"].isdigit() else 0)
    return attempts


def fetch_late_data() -> tuple[
    dict[tuple[str, int], float],
    dict[tuple[str, int], str],
    dict[int, tuple[str, str]],
    dict[int, dict[str, list[dict]]],
]:
    """Crawl Blackboard. Returns (late_map, submit_time_map, due_date_map, multi_attempts).

    multi_attempts: {col_idx: {sid: [attempt_dict, ...]}} — only students with
    attempts spanning multiple penalty zones.
    """
    print("Authenticating...")
    cl = get_session()
    course_id = settings.course_id
    if not course_id:
        raise RuntimeError("COURSE_ID not set in .env")

    crawler = PKUHomeworkCrawler(cl, course_id, set())
    late_map: dict[tuple[str, int], float] = {}
    submit_time_map: dict[tuple[str, int], str] = {}
    due_date_map: dict[int, tuple[str, str]] = {}
    multi_attempts: dict[int, dict[str, list[dict]]] = {}

    for col_idx, asst_name in sorted(ASSIGNMENT_NAMES.items()):
        gbpk = ASSIGNMENT_GBPK[asst_name]
        print(f"\nFetching {asst_name} (gradeBookPK={gbpk})...")

        due_str = MANUAL_DUE.get(gbpk) or crawler.fetch_due_date(gbpk, asst_name)
        due_date = parse_ts(due_str) if due_str else None
        if not due_date:
            print(f"  [WARN] No due date found, skipping")
            continue
        due_str_fmt = due_date.strftime('%Y-%m-%d %H:%M')
        due_date_map[col_idx] = (due_str_fmt, asst_name)
        print(f"  Due: {due_str_fmt}")

        resp = cl.get(
            f"{HW_BASE}/getStudentWork.do",
            params={"course_id": course_id, "gradeBookPK": gbpk, "title": asst_name, "showAll": "true"},
        )
        resp.raise_for_status()
        html = resp.text
        students = _parse_student_list(html, verbose=False)
        print(f"  Students: {len(students)}")

        # Parse ALL per-row attempts for multi-attempt analysis
        raw_attempts = _parse_per_row_attempts(html, due_date)

        late_count = 0
        for s in students:
            sid = s.get("userId", "")
            sub_str = s.get("submitted_at", "")
            if not sub_str:
                continue
            # Skip students with multiple attempts — they likely had an on-time
            # first attempt and re-submitted late. Penalizing would be unfair.
            if s.get("has_multiple_attempts", False):
                continue
            sub_dt = parse_ts(sub_str)
            if not sub_dt or sub_dt <= due_date:
                continue
            late_days = (sub_dt - due_date).total_seconds() / 86400.0
            late_map[(sid, col_idx)] = late_days
            submit_time_map[(sid, col_idx)] = sub_dt.strftime('%Y-%m-%d %H:%M')
            late_count += 1

        print(f"  Late submissions: {late_count}")

        # Identify cross-zone multi-attempt students
        cross_zone: dict[str, list[dict]] = {}
        for sid, atts in raw_attempts.items():
            if len(atts) <= 1:
                continue
            zones = set(a["zone"] for a in atts if a["zone"] != "?")
            # Extract zone prefixes (A, B, C)
            prefixes = set()
            for z in zones:
                if z.startswith("A"):
                    prefixes.add("A")
                elif z.startswith("B"):
                    prefixes.add("B")
                elif z.startswith("C"):
                    prefixes.add("C")
            if len(prefixes) >= 2:
                cross_zone[sid] = atts

        if cross_zone:
            multi_attempts[col_idx] = cross_zone
            print(f"  Cross-zone multi-attempt: {len(cross_zone)}")

    return late_map, submit_time_map, due_date_map, multi_attempts, cl, course_id


def fetch_attempt_grades(cl, course_id, multi_attempts: dict) -> dict[tuple[str, int, str], str]:
    """Fetch grades for '已批' attempts via CheckAloneWork.do.
    Returns {(sid, col_idx, attemptPk): grade_str}."""
    grades: dict[tuple[str, int, str], str] = {}
    total_fetched = 0
    for col_idx, cross_zone in multi_attempts.items():
        asst_name = ASSIGNMENT_NAMES.get(col_idx, "")
        gbpk = ASSIGNMENT_GBPK.get(asst_name, "")
        if not gbpk:
            continue
        for sid, atts in cross_zone.items():
            for a in atts:
                if a["graded"] != "已批":
                    continue
                apk = a["attemptPk"]
                fpk = a.get("filePk", "")
                if apk == "?" or fpk == "?":
                    continue
                try:
                    resp = cl.get(
                        f"{HW_BASE}/CheckAloneWork.do",
                        params={
                            "course_id": course_id, "gradeBookPK": gbpk,
                            "userId": sid, "filePk": fpk,
                            "title": asst_name, "attemptPk": apk,
                        },
                    )
                    resp.raise_for_status()
                    import re as _re
                    m = _re.search(r'id="inputBox"[^>]*value="([^"]*)"', resp.text)
                    if m and m.group(1).strip():
                        grades[(sid, col_idx, apk)] = m.group(1).strip()
                        total_fetched += 1
                except Exception:
                    pass
    if total_fetched:
        print(f"  Fetched grades for {total_fetched} graded attempts")
    return grades


def read_raw_csv(csv_path: str) -> tuple[list[str], list[list[str]]]:
    with open(csv_path, "rb") as f:
        raw = f.read()
    if raw[:2] == b'\xff\xfe':
        text = raw.decode("utf-16-le")
    else:
        text = raw.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = next(reader)
    rows = list(reader)
    return header, rows


def read_chengefei_csv() -> list[dict]:
    if not CHENGEFEI_CSV.exists():
        print(f"  [WARN] Chen GeFei CSV not found at {CHENGEFEI_CSV}")
        return []
    with open(CHENGEFEI_CSV, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        all_rows = list(reader)
    header = all_rows[9]
    data_rows = all_rows[10:]
    students = []
    for row in data_rows:
        if len(row) < 15:
            continue
        sid = row[0].strip()
        name = row[1].strip()
        pscj = parse_score(row[4]) if len(row) > 4 else None
        qzcj = parse_score(row[5]) if len(row) > 5 else None
        qmcj = parse_score(row[6]) if len(row) > 6 else None
        mid_raw = parse_score(row[7]) if len(row) > 7 else None
        mid_check = parse_score(row[8]) if len(row) > 8 else None
        total_raw = parse_score(row[9]) if len(row) > 9 else None
        hw_raw = parse_score(row[10]) if len(row) > 10 else None
        pct = parse_score(row[11]) if len(row) > 11 else None
        check_val = parse_score(row[12]) if len(row) > 12 else None
        unproc = parse_score(row[13]) if len(row) > 13 else None
        check2_val = parse_score(row[14]) if len(row) > 14 else None
        students.append({
            "sid": sid,
            "name": name,
            "PSCJ": pscj,
            "QZCJ": qzcj,
            "QMCJ": qmcj,
            "mid_raw": mid_raw,
            "mid_check": mid_check,
            "hw_raw": hw_raw,
            "pct": pct,
            "check": check_val,
            "unproc": unproc,
            "check2": check2_val,
        })
    return students


def read_filled_xls() -> list[dict]:
    """Read the generated _filled.xls and return PSCJ values per student."""
    if not FILLED_XLS.exists():
        print(f"  [WARN] Filled XLS not found at {FILLED_XLS}")
        return []
    import xlrd
    wb = xlrd.open_workbook(str(FILLED_XLS))
    ws = wb.sheet_by_index(0)
    students = []
    for r in range(2, ws.nrows):
        sid = str(ws.cell_value(r, 0)).strip()
        name = str(ws.cell_value(r, 1)).strip()
        pscj_val = ws.cell_value(r, 4)
        qzcj_val = ws.cell_value(r, 5)
        qmcj_val = ws.cell_value(r, 6)
        students.append({
            "sid": sid,
            "name": name,
            "PSCJ": float(pscj_val) if isinstance(pscj_val, (int, float)) and pscj_val != "" else None,
            "QZCJ": float(qzcj_val) if isinstance(qzcj_val, (int, float)) and qzcj_val != "" else None,
            "QMCJ": float(qmcj_val) if isinstance(qmcj_val, (int, float)) and qmcj_val != "" else None,
        })
    return students


def read_bonus_data() -> dict[str, float]:
    if not BONUS_XLS.exists():
        print(f"  [WARN] Bonus file not found at {BONUS_XLS}")
        return {}
    import xlrd
    wb = xlrd.open_workbook(str(BONUS_XLS))
    ws = wb.sheet_by_index(0)
    bonus: dict[str, float] = {}
    for r in range(2, ws.nrows):
        sid = str(ws.cell_value(r, 0)).strip()
        total = ws.cell_value(r, 9) if ws.ncols > 9 else 0
        if isinstance(total, (int, float)) and total > 0:
            bonus[sid] = total
    return bonus


def compute_penalty_breakdown(
    row: list[str],
    late_map: dict[tuple[str, int], float],
    submit_time_map: dict[tuple[str, int], str],
) -> tuple[list[tuple], list[tuple]]:
    sid = row[2].strip()
    penalties = []
    warns = []
    for c in HW_COLS:
        key = (sid, c)
        late_days = late_map.get(key)
        if late_days is None:
            continue
        asst_name = ASSIGNMENT_NAMES[c]
        score = parse_score(row[c])
        sub_time = submit_time_map.get(key, "")
        if score is None:
            warns.append((asst_name, late_days, sub_time))
            continue
        if late_days > LATE_ZERO_DAYS:
            if score > 0:
                penalties.append((asst_name, late_days, score, 0.0, score, sub_time))
        else:
            if score > LATE_CAP:
                deduction = score - LATE_CAP
                penalties.append((asst_name, late_days, score, float(LATE_CAP), deduction, sub_time))
    return penalties, warns


def download_multi_attempt_files(cl, course_id, multi_attempts_data, attempt_grades, due_date_map):
    """Download all multi-attempt cross-zone submissions and save with descriptive filenames."""
    from urllib.parse import quote

    out_dir = Path.home() / "Downloads" / "multi_attempt_submissions"
    out_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0

    for col_idx, cross_zone in multi_attempts_data.items():
        asst_name = ASSIGNMENT_NAMES.get(col_idx, "")
        gbpk = ASSIGNMENT_GBPK.get(asst_name, "")
        if not gbpk:
            continue

        for sid, atts in cross_zone.items():
            if sid.startswith("22"):
                continue
            name = atts[0]["name"]
            safe_name = re.sub(r"[^\w]", "_", name)
            short_name = re.sub(r"[^\w]", "_", asst_name)

            for i, a in enumerate(atts):
                apk = a["attemptPk"]
                fpk = a.get("filePk", "")
                zone_short = a["zone"].split("-")[0] if "-" in a["zone"] else a["zone"]
                if apk == "?" or fpk == "?":
                    continue

                label = f"{sid}_{safe_name}_{short_name}_第{i+1}次_{zone_short}"

                try:
                    do_type = "CheckAloneWork" if a["graded"] == "已批" else "CheckWork"
                    resp = cl.get(
                        f"{HW_BASE}/{do_type}.do",
                        params={
                            "course_id": course_id, "gradeBookPK": gbpk,
                            "userId": sid, "filePk": fpk,
                            "title": asst_name, "attemptPk": apk,
                        },
                    )
                    resp.raise_for_status()
                    m = re.search(r"(?:var|const) filePath = '([^']+)'", resp.text)
                    if not m:
                        continue
                    file_path = m.group(1)
                    ext = Path(file_path).suffix or ".bin"
                    out_file = out_dir / f"{label}{ext}"
                    if out_file.exists():
                        continue
                    encoded = quote(quote(file_path, safe=""), safe="")
                    file_resp = cl.get(f"{HW_BASE}/api/pdf.do?path={encoded}")
                    file_resp.raise_for_status()
                    out_file.write_bytes(file_resp.content)
                    downloaded += 1
                except Exception:
                    pass

    if downloaded:
        print(f"  Downloaded {downloaded} multi-attempt files to {out_dir}")


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)

    csv_path = sys.argv[1]
    args = sys.argv[2:]
    out_path = "晚交扣分与校验综合报告.md"
    do_download = False
    for a in args:
        if a == "--download":
            do_download = True
        elif not a.startswith("--"):
            out_path = a

    # Step 1: Crawl late data
    print("=" * 60)
    print("Step 1: Crawling late submission data...")
    print("=" * 60)
    late_map, submit_time_map, due_date_map, multi_attempts_data, bb_client, course_id_for_grades = fetch_late_data()
    # Fetch grades for 已批 attempts
    attempt_grades = fetch_attempt_grades(bb_client, course_id_for_grades, multi_attempts_data)
    print(f"\nTotal late records: {len(late_map)}")

    # Step 2: Read raw CSV
    print("\n" + "=" * 60)
    print("Step 2: Reading raw CSV scores...")
    print("=" * 60)
    header, rows = read_raw_csv(csv_path)
    # Filter out students with IDs starting with "22"
    rows = [r for r in rows if not r[2].strip().startswith("22")]
    print(f"Students in CSV (after filtering): {len(rows)}")

    student_names: dict[str, str] = {}
    raw_by_sid: dict[str, list[str]] = {}
    for row in rows:
        sid = row[2].strip()
        name = row[1].strip()
        student_names[sid] = name
        raw_by_sid[sid] = row

    # Step 3: Compute penalties
    print("\n" + "=" * 60)
    print("Step 3: Computing late penalties...")
    print("=" * 60)

    penalties: dict[str, list[tuple[str, float, float, float, float, str]]] = {}
    warnings_list: dict[str, list[tuple[str, float, str]]] = {}

    for row in rows:
        sid = row[2].strip()
        p, w = compute_penalty_breakdown(row, late_map, submit_time_map)
        if p:
            p.sort(key=lambda x: x[1], reverse=True)
            penalties[sid] = p
        if w:
            warnings_list[sid] = w

    # Step 4: Read XLS data (newly generated with fixes) and bonus data
    print("\n" + "=" * 60)
    print("Step 4: Reading XLS and bonus data...")
    print("=" * 60)

    xls_students = read_filled_xls()
    xls_by_sid: dict[str, dict] = {s["sid"]: s for s in xls_students}
    print(f"Students in XLS: {len(xls_students)}")

    bonus_data = read_bonus_data()
    print(f"Students with bonus: {len(bonus_data)}")

    # Build student data for jiaoyan reconciliation from raw CSV + XLS
    # pct = raw percentage from CSV scores (before penalties)
    # PSCJ = from XLS (our corrected output)
    reconciliation_students = []
    for row in rows:
        sid = row[2].strip()
        hw_scores = []
        for c in HW_COLS:
            s = parse_score(row[c])
            hw_scores.append(s)
        hw_sum = sum(sc for sc in hw_scores if sc is not None)
        pct = hw_sum / NUM_HW if hw_sum > 0 else None
        xls_entry = xls_by_sid.get(sid, {})
        reconciliation_students.append({
            "sid": sid,
            "name": student_names.get(sid, "?"),
            "pct": pct,
            "PSCJ": xls_entry.get("PSCJ"),
            "QZCJ": xls_entry.get("QZCJ"),
            "mid_raw": parse_score(row[MIDTERM_COL]) if MIDTERM_COL < len(row) else None,
            "mid_check": None,
            "check": None,  # computed below
            "unproc": None,  # computed below
            "check2": None,
        })

    # Step 5: Generate Markdown report
    print("\n" + "=" * 60)
    print(f"Step 5: Writing report to {out_path}...")
    print("=" * 60)

    total_penalized = len(penalties)
    total_deductions = sum(item[4] for items in penalties.values() for item in items)
    zeroed_count = sum(1 for items in penalties.values() for item in items if abs(item[3]) < 0.001 or abs(item[4] - item[2]) < 0.001)
    capped_count = sum(1 for items in penalties.values() for item in items if 0.001 < item[4] < item[2])

    lines = []
    lines.append("# 晚交扣分与校验综合报告")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now(BEIJING_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> 数据来源：Blackboard 爬取 + 原始CSV导出 + 陈格非期末总评 + 加分文件")
    lines.append(f"> 扣分规则：>7天 → 0分，≤7天且分数>90 → 上限90分")
    lines.append("")

    # Due dates
    lines.append("## 各作业截止时间")
    lines.append("")
    lines.append("| 作业 | 截止时间 |")
    lines.append("|------|---------|")
    for col_idx in sorted(due_date_map):
        due_str, asst_name = due_date_map[col_idx]
        lines.append(f"| {asst_name} | {due_str} |")
    lines.append("")

    # Summary
    lines.append("## 晚交扣分总览")
    lines.append("")
    lines.append(f"- **被扣分学生数**：{total_penalized} 人")
    lines.append(f"- **总扣分次数**：{sum(len(items) for items in penalties.values())} 次")
    lines.append(f"- **累计扣分**：{total_deductions:.1f} 分")
    lines.append(f"- >7天置零：{zeroed_count} 次")
    lines.append(f"- ≤7天上限90：{capped_count} 次")
    lines.append("")

    sorted_students = sorted(
        penalties.items(),
        key=lambda x: sum(item[4] for item in x[1]),
        reverse=True,
    )

    lines.append("### 学生扣分一览")
    lines.append("")
    lines.append("| 学号 | 姓名 | 扣分合计 | 涉及作业 |")
    lines.append("|------|------|---------|---------|")
    for sid, items in sorted_students:
        name = student_names.get(sid, "?")
        total = sum(item[4] for item in items)
        assignments_str = "; ".join(item[0] for item in items[:3])
        if len(items) > 3:
            assignments_str += f" 等{len(items)}次"
        lines.append(f"| {sid} | {name} | **{total:.0f}** | {assignments_str} |")
    lines.append("")

    # Per-student detail
    lines.append("## 逐人详细扣分记录")
    lines.append("")
    for sid, items in sorted_students:
        name = student_names.get(sid, "?")
        total = sum(item[4] for item in items)
        lines.append(f"### {name}（{sid}）— 共扣 {total:.0f} 分")
        lines.append("")
        lines.append("| 作业 | 截止时间 | 提交时间 | 晚交天数 | 原始分数 | 处理后分数 | 扣分 |")
        lines.append("|------|---------|---------|---------|---------|-----------|------|")
        for item in items:
            asst, late_days, orig, new, ded, sub_time = item
            late_str = f"{late_days:.1f}天"
            due_str = due_date_map.get(
                [c for c, n in ASSIGNMENT_NAMES.items() if n == asst][0], ("?", "")
            )[0]
            rule = ">7天置零" if late_days > LATE_ZERO_DAYS else "≤7天上限90"
            lines.append(
                f"| {asst} | {due_str} | {sub_time} | {late_str} "
                f"| {orig:.0f} | {new:.0f} | **-{ded:.0f}**（{rule}） |"
            )
        lines.append("")

    # Warnings
    if warnings_list:
        lines.append("---")
        lines.append("")
        lines.append("## 异常记录（晚交但无原始分数）")
        lines.append("")
        lines.append("| 学号 | 姓名 | 作业 | 截止时间 | 提交时间 | 晚交天数 |")
        lines.append("|------|------|------|---------|---------|---------|")
        for sid, warns in sorted(warnings_list.items(), key=lambda x: -len(x[1])):
            name = student_names.get(sid, "?")
            for asst, late_days, sub_time in warns:
                due_str = due_date_map.get(
                    [c for c, n in ASSIGNMENT_NAMES.items() if n == asst][0], ("?", "")
                )[0]
                lines.append(
                    f"| {sid} | {name} | {asst} | {due_str} | {sub_time} | {late_days:.1f}天 |"
                )
        lines.append("")

    # =========================================================
    # Section B: jiaoyan reconciliation
    # =========================================================
    lines.append("---")
    lines.append("")
    lines.append("# 作业校验（jiaoyan）综合解释")
    lines.append("")
    lines.append("> 校验 = PSCJ_平时成绩 - 百分制（原始作业总分/9，来自未扣分的Blackboard导出）。")
    lines.append("> 四个因素：")
    lines.append("> 1. **晚交扣分效应**（负值）：-总扣分/9，降低作业平均分")
    lines.append("> 2. **取整效应**（正值，较小）：ceil(扣分后平均分) - 扣分后平均分")
    lines.append("> 3. **加分效应**（正值）：加分/0.2 加到平时分后再取整")
    lines.append("> 4. **手动调整**：陈格非直接调整（如王思明+15，顾锦松+12）")
    lines.append("")

    # Compute check/unproc for each reconciliation student
    for s in reconciliation_students:
        sid = s["sid"]
        pct = s["pct"]
        has_late = sid in penalties
        if has_late:
            total_late_ded = sum(item[4] for item in penalties[sid])
            late_avg_effect = -total_late_ded / 9.0
        else:
            late_avg_effect = 0
        penalized_avg = (pct or 0) + late_avg_effect
        s["unproc"] = _round_score(penalized_avg) if penalized_avg > 0 else None
        s["_late_avg_effect"] = late_avg_effect
        s["_penalized_avg"] = penalized_avg
        # mid_check: diff between XLS QZCJ and raw midterm
        if s.get("QZCJ") is not None and s.get("mid_raw") is not None:
            mt = s["QZCJ"] - s["mid_raw"]
            s["mid_check"] = mt if abs(mt) > 0.01 else 0
        else:
            s["mid_check"] = 0
        # check = PSCJ - pct
        if s.get("PSCJ") is not None and pct is not None:
            s["check"] = s["PSCJ"] - pct
        else:
            s["check"] = None

    check_candidates = [s for s in reconciliation_students if s["check"] is not None and abs(s["check"]) >= 1]
    check_candidates.sort(key=lambda s: -abs(s["check"]))

    lines.append(f"## |校验| ≥ 1 的学生（共 {len(check_candidates)} 人）")
    lines.append("")
    lines.append("| 学号 | 姓名 | 百分制 | 未处理 | PSCJ | 校验 | 晚交效应 | 取整 | 加分 | 加分效应 | 手动调整 | 期中校验 | 说明 |")
    lines.append("|------|------|-------|--------|------|--------|---------|------|------|---------|---------|---------|------|")

    for s in check_candidates:
        sid = s["sid"]
        name = s["name"]
        pct = s["pct"]
        unproc = s["unproc"]
        pscj = s["PSCJ"]
        check_val = s["check"]
        mid_check = s["mid_check"]

        has_late = sid in penalties
        total_late_ded = sum(item[4] for item in penalties[sid]) if has_late else 0
        late_avg_effect = s.get("_late_avg_effect", 0)
        penalized_avg = s.get("_penalized_avg", (pct or 0))

        # Ceil effect: unproc - penalized_avg.
        # When penalized_avg > unproc (gap negative >0.5), we have undetected penalties.
        # In that case, the full gap is attributed to undetected penalty (can't separate ceil).
        undetected_effect = 0.0
        if unproc is not None and penalized_avg is not None:
            raw_gap = unproc - penalized_avg
            if raw_gap < -0.5 and not has_late:
                undetected_effect = round(raw_gap, 2)
                ceil_effect = 0.0
            else:
                ceil_effect = round(raw_gap, 2)
                undetected_effect = 0.0
        else:
            ceil_effect = None
            undetected_effect = 0.0

        # Bonus effect
        bonus = bonus_data.get(sid, 0)
        bonus_hw_points = bonus / 0.2 if bonus > 0 else 0
        if bonus_hw_points > 0 and unproc is not None:
            expected_after_bonus = _round_score(unproc + bonus_hw_points)
            bonus_effect = expected_after_bonus - unproc
        else:
            bonus_effect = 0

        # Manual adjustment
        if pscj is not None and unproc is not None and bonus_effect is not None:
            manual = pscj - unproc - bonus_effect
            if abs(manual) < 0.01:
                manual = 0
        else:
            manual = None

        # Verify: late_avg_effect + undetected_effect + ceil_effect + bonus_effect + manual ≈ check_val
        check_components = (late_avg_effect or 0) + undetected_effect + (ceil_effect or 0) + (bonus_effect or 0) + (manual or 0)
        if abs(check_components - (check_val or 0)) > 0.05:
            # Residual: what's left unexplained
            residual = (check_val or 0) - check_components
            if abs(residual) > 0.05 and manual is not None:
                manual += residual

        # Build explanation
        explanations = []
        if has_late:
            explanations.append(f"late -{total_late_ded:.0f}")
        elif undetected_effect < -0.01:
            explanations.append(f"undetected penalty ~{undetected_effect:.0f}")
        if bonus > 0:
            explanations.append(f"bonus {bonus}/{bonus_hw_points:.1f}+{bonus_effect}")
        if manual is not None and manual > 0.01:
            explanations.append(f"manual +{manual:.0f}")
        elif manual is not None and manual < -0.01:
            explanations.append(f"manual {manual:.0f}")
        if not explanations and ceil_effect is not None:
            explanations.append("ceil only")

        exp_str = "; ".join(explanations)

        # Translate explanation to Chinese
        exp_cn = exp_str
        exp_cn = exp_cn.replace("late -", "晚扣-")
        exp_cn = exp_cn.replace("undetected penalty ~", "==未检扣==")
        exp_cn = exp_cn.replace("bonus ", "加分")
        exp_cn = exp_cn.replace("manual +", "手动+")
        exp_cn = exp_cn.replace("manual ", "手动")
        exp_cn = exp_cn.replace("ceil only", "仅取整")
        # Fix bonus format like "加分1.0/5.0+5.0" -> "加分1.0(5.0)+5"
        if "加分" in exp_cn and "/" in exp_cn:
            parts = exp_cn.split("; ")
            new_parts = []
            for p in parts:
                if p.startswith("加分"):
                    # bonus 1.0/5.0+5.0 -> 加分1.0
                    p = p.split("/")[0]
                new_parts.append(p)
            exp_cn = "; ".join(new_parts)

        pct_s = f"{pct:.2f}" if pct is not None else "?"
        unproc_s = f"{unproc:.0f}" if unproc is not None else "?"
        pscj_s = f"{pscj:.0f}" if pscj is not None else "?"
        cv_s = f"{check_val:.2f}" if check_val is not None else "?"
        le_s = f"{late_avg_effect:.2f}" if has_late else "0"
        ce_s = f"{ceil_effect:.2f}" if ceil_effect is not None else "?"
        bs = str(bonus)
        be_s = str(bonus_effect)
        mn_s = f"{manual:.0f}" if manual is not None and manual != 0 else "0"
        mc_s = f"{mid_check:.0f}" if mid_check is not None and abs(mid_check) > 0.01 else "0"

        lines.append(f"| {sid} | {name} | {pct_s} | {unproc_s} | {pscj_s} | {cv_s} | {le_s} | {ce_s} | {bs} | {be_s} | {mn_s} | {mc_s} | {exp_cn} |")
    lines.append("")

    # Detailed case studies
    interesting = [s for s in check_candidates if (
        s["check"] is not None and abs(s["check"]) >= 3
    )]
    if interesting:
        lines.append("## 重点案例分析")
        lines.append("")
        for s in interesting:
            sid = s["sid"]
            name = s["name"]
            pct = s["pct"]
            unproc = s["unproc"]
            pscj = s["PSCJ"]
            check_val = s["check"]
            bonus = bonus_data.get(sid, 0)
            bonus_hw_points = bonus / 0.2 if bonus > 0 else 0
            has_late = sid in penalties
            mid_check = s["mid_check"]

            total_late_ded = sum(item[4] for item in penalties[sid]) if has_late else 0
            late_avg_effect = s.get("_late_avg_effect", 0)
            penalized_avg = s.get("_penalized_avg", (pct or 0))
            raw_gap = (unproc or 0) - penalized_avg
            if raw_gap < -0.5 and not has_late:
                undetected_penalty = round(raw_gap, 2)
                ceil_effect = 0
            else:
                undetected_penalty = 0
                ceil_effect = raw_gap

            lines.append(f"### {name}（{sid}）")
            lines.append("")
            lines.append(f"- 百分制（未扣分）={pct:.4f}，扣分后平均≈{penalized_avg:.4f}，未处理={unproc:.0f}，PSCJ={pscj:.0f}")
            lines.append(f"- 校验={check_val:.4f}")
            if has_late:
                lines.append(f"- 晚交扣分：-{total_late_ded:.0f} 总分，-{total_late_ded}/9 = {late_avg_effect:.4f} 平均效应")
                lines.append(f"- 取整：ceil({penalized_avg:.4f}) = {unproc:.0f}，取整效应 = +{ceil_effect:.4f}")
            elif undetected_penalty < -0.01:
                lines.append(f"- **==未检出扣分==（多次提交或无原始分）≈{undetected_penalty:.0f}**")
                lines.append(f"- 取整效应无法分离（包含在==未检出扣分==中）")
            else:
                lines.append(f"- 取整：ceil({penalized_avg:.4f}) = {unproc:.0f}，取整效应 = +{ceil_effect:.4f}")
            if bonus > 0:
                expected = _round_score(unproc + bonus_hw_points)
                lines.append(f"- 加分={bonus}，加分/0.2 = {bonus_hw_points:.2f}")
                lines.append(f"- 加分后：ceil({unproc:.0f} + {bonus_hw_points:.2f}) = {expected}")
                bonus_effect = expected - unproc
                lines.append(f"- 加分效应 = +{bonus_effect}")
                remainder = pscj - expected
                if abs(remainder) > 0.01:
                    lines.append(f"- **手动调整 = {pscj} - {expected} = {remainder:.0f}**")
                else:
                    lines.append(f"- 无手动调整")
            else:
                if pscj != unproc:
                    lines.append(f"- 无加分，但 PSCJ({pscj}) ≠ 未处理({unproc})")
                    remainder = pscj - unproc
                    lines.append(f"- **手动调整 = {remainder:.0f}**")
                else:
                    lines.append(f"- 无加分，PSCJ = 未处理")
            total_check = late_avg_effect + ceil_effect
            if bonus > 0:
                bonus_effect = _round_score(unproc + bonus_hw_points) - unproc
                total_check += bonus_effect
            if pscj != (unproc or 0) + (bonus_effect if bonus > 0 else 0):
                total_check += pscj - (unproc or 0) - (bonus_effect if bonus > 0 else 0)
            lines.append(f"- 分解：{late_avg_effect:.4f}(晚交) + {ceil_effect:.4f}(取整) = {late_avg_effect + ceil_effect:.4f}（加分/手动前）")
            if mid_check is not None and abs(mid_check) > 0.01:
                lines.append(f"- 期中校验 = {mid_check}：期中成绩也有调整")
            lines.append("")

    # Multi-attempt section
    if multi_attempts_data:
        lines.append("---")
        lines.append("")
        lines.append("## 多次提交记录（按作业）")
        lines.append("")
        lines.append("> 多次提交的学生不应扣分（至少有一次按时提交）。")
        lines.append("> A=按时, B=≤7d上限90, C=>7d零分。")
        lines.append("> ⚠️ 跨区间=尝试分布在超过一个扣分区间中，这些学生的晚交扣分已被跳过。")
        lines.append("")

        total_cross = 0
        for col_idx in sorted(multi_attempts_data):
            cross_zone = multi_attempts_data[col_idx]
            if not cross_zone:
                continue
            due_str, asst_name = due_date_map.get(col_idx, ("?", "?"))
            total_cross += len(cross_zone)

            lines.append(f"### {asst_name}（截止：{due_str}）— {len(cross_zone)} 人跨区间")
            lines.append("")
            lines.append("| 学号 | 姓名 | 次数 | 区间 | 各次提交详情 |")
            lines.append("|------|------|------|------|-------------|")
            for sid in sorted(cross_zone):
                if sid.startswith("22"):
                    continue  # exclude 22xxx students
                atts = cross_zone[sid]
                name = atts[0]["name"]
            # Get zone distribution
                zone_counts = {}
                for a in atts:
                    z = a["zone"]
                    prefix = "A" if z.startswith("A") else ("B" if z.startswith("B") else ("C" if z.startswith("C") else "?"))
                    zone_counts[prefix] = zone_counts.get(prefix, 0) + 1
                zone_summary = ", ".join(f"{k}×{v}" for k, v in sorted(zone_counts.items()))
                # Build attempt details string with grades
                attempt_lines = []
                for i, a in enumerate(atts):
                    grade_key = (sid, col_idx, a["attemptPk"])
                    grade = attempt_grades.get(grade_key, "")
                    if grade and a["graded"] == "已批":
                        detail = f"#{i+1} {a['sub_time_str']} [{a['zone']}] (已批 {grade}分)"
                    else:
                        detail = f"#{i+1} {a['sub_time_str']} [{a['zone']}] ({a['graded']})"
                    attempt_lines.append(detail)
                attempts_str = "<br>".join(attempt_lines)
                lines.append(f"| {sid} | {name} | {len(atts)} | {zone_summary} ⚠️ | {attempts_str} |")
            lines.append("")

        print(f"  Multi-attempt cross-zone entries in report: {total_cross}")

        if do_download:
            download_multi_attempt_files(bb_client, course_id_for_grades,
                                         multi_attempts_data, attempt_grades, due_date_map)

        # Build per-student summary
        # Collect all cross-zone students with their assignment details
        student_cross_info: dict[str, dict] = {}  # sid -> {name, assignments: {col_idx: info}}
        for col_idx in sorted(multi_attempts_data):
            cross_zone = multi_attempts_data[col_idx]
            asst_name = ASSIGNMENT_NAMES.get(col_idx, "?")
            for sid, atts in cross_zone.items():
                if sid.startswith("22"):
                    continue
                if sid not in student_cross_info:
                    student_cross_info[sid] = {"name": atts[0]["name"], "assignments": {}}
                student_cross_info[sid]["assignments"][col_idx] = {
                    "asst_name": asst_name,
                    "attempts": atts,
                }

        if student_cross_info:
            lines.append("---")
            lines.append("")
            lines.append("## 逐人多次提交总结")
            lines.append("")
            lines.append("> 对比两种给分方式下的PSCJ：")
            lines.append("> - **方式A（扣迟交）**：对迟交作业按规则扣分（>7d置零、≤7d上限90）后计算")
            lines.append("> - **方式B（不扣）**：保留按时提交的有效分数，不因后续迟交重提交而扣分")
            lines.append("")

            for sid in sorted(student_cross_info):
                info = student_cross_info[sid]
                name = info["name"]
                raw_row = raw_by_sid.get(sid)
                if not raw_row:
                    continue

                # Get all raw homework scores
                hw_scores = {}
                hw_penalized = {}
                for c in HW_COLS:
                    score = parse_score(raw_row[c]) if len(raw_row) > c else None
                    hw_scores[c] = score
                    hw_penalized[c] = score

                # Apply late penalties (Method A) for cross-zone assignments
                late_assignments = []
                for col_idx in info["assignments"]:
                    asst_info = info["assignments"][col_idx]
                    asst_name = asst_info["asst_name"]
                    atts = asst_info["attempts"]
                    score = hw_scores.get(col_idx)

                    # Determine zones present
                    zones = set(a["zone"] for a in atts if a["zone"] != "?")
                    has_A = any(z.startswith("A") for z in zones)
                    has_B = any(z.startswith("B") for z in zones)
                    has_C = any(z.startswith("C") for z in zones)

                    # Get graded attempt scores
                    graded_scores = []
                    for a in atts:
                        gk = (sid, col_idx, a["attemptPk"])
                        g = attempt_grades.get(gk, "")
                        if g and a["graded"] == "已批":
                            graded_scores.append((a["zone"], int(float(g)) if g.replace(".","").isdigit() else g))

                    # Method A: compute late_days from attempt data (not late_map, which skips multi-attempt)
                    due_str, _ = due_date_map.get(col_idx, ("", ""))
                    due_dt = parse_ts(due_str) if due_str else None
                    # Use the LATEST attempt's submission time (what BB system sees)
                    latest_sub_dt = max(
                        (a["sub_dt"] for a in atts if a["sub_dt"] is not None),
                        default=None
                    )
                    late_days_a = None
                    if due_dt and latest_sub_dt and latest_sub_dt > due_dt:
                        late_days_a = (latest_sub_dt - due_dt).total_seconds() / 86400.0

                    penalty_a = 0
                    if late_days_a is not None and score is not None:
                        if late_days_a > LATE_ZERO_DAYS and score > 0:
                            penalty_a = score  # zeroed
                        elif late_days_a <= LATE_ZERO_DAYS and score > LATE_CAP:
                            penalty_a = score - LATE_CAP

                    # Method B: keep original score (no penalty for multi-attempt)
                    score_b = score if score is not None else 0

                    late_assignments.append({
                        "asst_name": asst_name,
                        "zones": zones,
                        "has_A": has_A,
                        "raw_score": score,
                        "late_days_a": late_days_a,
                        "penalty_a": penalty_a,
                        "score_after_a": (score - penalty_a) if score is not None else None,
                        "score_b": score_b,
                        "graded_scores": graded_scores,
                        "attempts": atts,
                    })

                # Compute Method A PSCJ
                valid_a = [s for s in hw_penalized.values() if s is not None]
                for la in late_assignments:
                    if la["score_after_a"] is not None:
                        col_idx_found = [c for c in info["assignments"] if info["assignments"][c]["asst_name"] == la["asst_name"]]
                        if col_idx_found:
                            hw_penalized[col_idx_found[0]] = la["score_after_a"]
                valid_a_actual = [s for s in hw_penalized.values() if s is not None]
                avg_a = sum(valid_a_actual) / NUM_HW
                pscj_a = _round_score(avg_a)
                # Add bonus
                bonus = bonus_data.get(sid, 0)
                if bonus > 0:
                    pscj_a_with_bonus = min(_round_score(pscj_a + bonus / 0.2), 100)
                else:
                    pscj_a_with_bonus = pscj_a

                # Compute Method B PSCJ (no penalties)
                valid_b = [s for s in hw_scores.values() if s is not None]
                avg_b = sum(valid_b) / NUM_HW
                pscj_b = _round_score(avg_b)
                if bonus > 0:
                    pscj_b_with_bonus = min(_round_score(pscj_b + bonus / 0.2), 100)
                else:
                    pscj_b_with_bonus = pscj_b

                # PSCJ from XLS
                xls_entry = xls_by_sid.get(sid, {})
                xls_pscj = xls_entry.get("PSCJ", "?")

                lines.append(f"### {name}（{sid}）")
                lines.append("")
                lines.append(f"- **跨区间作业**：{len(late_assignments)} 次")
                for la in late_assignments:
                    zone_str = ", ".join(sorted(la["zones"]))
                    lines.append(f"  - {la['asst_name']}：{len(la['attempts'])}次提交（{zone_str}）")
                    for a in la["attempts"]:
                        gk = (sid, [c for c in info["assignments"] if info["assignments"][c]["asst_name"] == la["asst_name"]][0], a["attemptPk"])
                        g = attempt_grades.get(gk, "")
                        g_str = f" {g}分" if g and a["graded"] == "已批" else ""
                        lines.append(f"    - #{a['attemptPk']} {a['sub_time_str']} [{a['zone']}] ({a['graded']}{g_str})")
                    if la["penalty_a"] > 0:
                        rule = ">7d置零" if (la["late_days_a"] or 0) > LATE_ZERO_DAYS else "≤7d上限90"
                        lines.append(f"    - 原始分={la['raw_score']:.0f}，迟交{la['late_days_a']:.1f}d，方式A：{rule}→扣{la['penalty_a']:.0f}分={la['score_after_a']:.0f}，方式B保留={la['score_b']:.0f}")
                    elif la["late_days_a"] is not None and la["late_days_a"] > 0:
                        lines.append(f"    - 原始分={la['raw_score']:.0f}，迟交{la['late_days_a']:.1f}d，但无需扣分（分数已≤上限），方式A=B={la['score_b']:.0f}")
                lines.append("")

                bonus_str = f" + 加分{bonus}(+{pscj_b_with_bonus - pscj_b:.0f})" if bonus > 0 else ""
                lines.append(f"- **方式A（扣迟交）PSCJ**：{pscj_a:.0f}{'（加分后' + str(pscj_a_with_bonus) + '）' if bonus > 0 else ''}")
                lines.append(f"- **方式B（不扣）PSCJ**：{pscj_b:.0f}{bonus_str} → {pscj_b_with_bonus:.0f}")
                lines.append(f"- **XLS实际PSCJ**：{xls_pscj}")
                diff = pscj_b_with_bonus - pscj_a_with_bonus
                lines.append(f"- **差额**：方式B比方式A高 {diff:.0f} 分")
                lines.append("")

    # Bonus table
    if bonus_data:
        lines.append("---")
        lines.append("")
        lines.append("## 奖励加分明细表")
        lines.append("")
        lines.append("> 加分来源：校内赛加分（col 7）、课堂回答加分（col 8）、总加分（col 9）。")
        lines.append("> 加分规则：优先加到平时成绩（权重0.2），加不上去再加到期中成绩（权重0.3）。")
        lines.append("> 加分效应 ≈ ceil(总加分/0.2) 或 ceil(剩余/0.3)。")
        lines.append("")

        import xlrd
        bonus_wb = xlrd.open_workbook(str(BONUS_XLS))
        bonus_ws = bonus_wb.sheet_by_index(0)

        lines.append("| 学号 | 姓名 | 校内赛加分 | 课堂回答加分 | 总加分 | 平时效应 | 期中效应 |")
        lines.append("|------|------|-----------|-------------|-------|---------|---------|")

        # Sort by total bonus descending
        bonus_rows = []
        for r in range(2, bonus_ws.nrows):
            sid = str(bonus_ws.cell_value(r, 0)).strip()
            contest = bonus_ws.cell_value(r, 7) if bonus_ws.ncols > 7 else 0
            qa = bonus_ws.cell_value(r, 8) if bonus_ws.ncols > 8 else 0
            total = bonus_ws.cell_value(r, 9) if bonus_ws.ncols > 9 else 0
            if not isinstance(total, (int, float)) or total <= 0:
                continue
            contest = contest if isinstance(contest, (int, float)) else 0
            qa = qa if isinstance(qa, (int, float)) else 0
            name = str(bonus_ws.cell_value(r, 1)).strip()
            bonus_rows.append((sid, name, contest, qa, total))

        bonus_rows.sort(key=lambda x: -x[4])

        # Build lookup for bonus effects
        recon_by_sid: dict[str, dict] = {s["sid"]: s for s in reconciliation_students}

        for sid, name, contest, qa, total in bonus_rows:
            if sid.startswith("22"):
                continue  # exclude 22xxx students
            s = recon_by_sid.get(sid)
            xls_s = xls_by_sid.get(sid)
            hw_effect = "?"
            mid_effect = "?"
            if s:
                unproc = s.get("unproc")
                pscj = xls_s.get("PSCJ") if xls_s else s.get("PSCJ")
                mid_raw = s.get("mid_raw")
                qzcj = xls_s.get("QZCJ") if xls_s else s.get("QZCJ")
                if unproc is not None and pscj is not None:
                    bonus_hw = total / 0.2
                    expected_hw = _round_score(unproc + bonus_hw) if unproc is not None else None
                    if expected_hw is not None:
                        eff = expected_hw - unproc
                        if eff > 0:
                            hw_effect = f"+{eff:.0f}"
                        else:
                            hw_effect = "0"
                    if expected_hw is not None and pscj > expected_hw:
                        # Bonus spilled over or manual
                        pass
                    # Midterm effect
                    if mid_raw is not None and qzcj is not None and qzcj > mid_raw:
                        mid_effect = f"+{qzcj - mid_raw:.0f}"
                    else:
                        mid_effect = "0"

            lines.append(f"| {sid} | {name} | {contest} | {qa} | **{total}** | {hw_effect} | {mid_effect} |")

        lines.append("")

    lines.append("---")
    lines.append("*由 generate_late_report.py 自动生成*")

    report = "\n".join(lines) + "\n"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(f"\nReport written to: {out_path}")
    print(f"Total students penalized: {total_penalized}")
    print(f"Total deductions: {total_deductions:.0f} points")
    print(f"Students with |jiaoyan|>=1: {len(check_candidates)}")


if __name__ == "__main__":
    main()
