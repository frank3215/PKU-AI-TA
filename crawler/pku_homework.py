"""
Crawler for PKU's custom homework system: bb-homeWorkCheck-BBLEARN.

Endpoints (all under /webapps/bb-homeWorkCheck-BBLEARN/homeWorkCheck/):

  getHomeWorkList.do?course_id=...
      → HTML page listing all assignments with gradeBookPK (numeric) and title

  getStudentWork.do?course_id=...&gradeBookPK=...&title=...&showAll=true
      → HTML page listing every submitted student:
          userId (real student number), name, filePk, attemptPk
        Links use CheckWork.do per student.

  CheckWork.do?course_id=...&gradeBookPK=...&userId=...&filePk=...&title=...&attemptPk=...
      → HTML page for one student's submission.
        JS embeds: var filePath = '/usr/local/blackboard/content/storage/pdf/{courseId}/{gradeBookPK}/{filePk}/{filename}'

  api/pdf.do?path={double_url_encoded_filePath}
      → Serves the PDF directly (application/pdf).
        NOTE: pass the double-encoded path directly in the URL string —
        do NOT use httpx params= which would triple-encode it.

  downloadBatch.do?course_id=...&gradeBookPK=...&isGroup=false
      → ZIP of all submitted files in the same order as getStudentWork.do.
        Used as a fast alternative to per-student fetching when no whitelist.

BB REST API is used to map student number → BB internal user ID
(needed for grade submission via PATCH gradebook/columns/{col}/users/{uid}).
"""
from __future__ import annotations

import io
import re
import zipfile
from urllib.parse import quote, unquote

import httpx

from models import Attachment, Submission

HW_BASE = "/webapps/bb-homeWorkCheck-BBLEARN/homeWorkCheck"
BB_API = "/learn/api/public/v1"

# getStudentWork.do has three submission link formats:
#   1. href="...CheckAloneWork.do?...">查看</a>   (already graded)
#   2. href="...CheckWork.do?...">批改</a>       (needs grading)
#   3. onclick="checkWork('userId','filePk','attemptPk',...)">批改</a> (needs grading)
_STUDENT_PATTERN = re.compile(
    r'<a[^>]*(?:CheckAloneWork|CheckWork)\.do\?course_id=[^&]+&gradeBookPK=(\d+)'
    r'&userId=(\d+)&filePk=(\d+)&title=([^&"]+)&attemptPk=(\d+)[^>]*>([^<]+)</a>'
)
# Flexible: checkWork may have 3+ parameters; we only need the first 3.
_STUDENT_ONCLICK_PATTERN = re.compile(
    r"""onclick=['"][^"]*checkWork\(\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'(\d+)'\s*[^)]*\)"""
    r"""[^>]*>([^<]+)</a>"""
)
_NAME_PATTERN = re.compile(
    r'scope="row"[^>]*>\s*(\d{10})\s*</th>.*?table-data-cell-value">(.*?)</span>',
    re.DOTALL,
)
# Matches submitted_at for each student row in getStudentWork.do HTML.
_SUBMITTED_AT_PATTERN = re.compile(
    r'scope="row"[^>]*>\s*(\d{10})\s*</th>.*?提交时间[:\s]*</span>\s*<span[^>]*>([\d\-:\s]+)</span>',
    re.DOTALL,
)
_FILE_PATH_PATTERN = re.compile(r"(?:var|const) filePath = '([^']+)'")


class PKUHomeworkCrawler:
    def __init__(self, client: httpx.Client, course_id: str, whitelist: set[str]):
        self.client = client
        self.course_id = course_id
        self.whitelist = whitelist
        self._bb_user_map: dict[str, str] = {}  # student_number → bb_internal_user_id

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch_assignments(self) -> list[dict]:
        """Return list of assignments: [{id, name, gradeBookPK}, ...]."""
        resp = self.client.get(
            f"{HW_BASE}/getHomeWorkList.do",
            params={"course_id": self.course_id},
        )
        resp.raise_for_status()
        assignments = _parse_homework_list(resp.text)
        assignments.reverse()  # newest first
        return assignments

    def count_submissions(self, grade_book_pk: str, title: str) -> dict:
        """Return submission counts for one assignment without downloading files.

        If a whitelist is set, only counts students in the whitelist.
        Returns {"total": int, "graded": int, "ungraded": int}
        """
        resp = self.client.get(
            f"{HW_BASE}/getStudentWork.do",
            params={
                "course_id": self.course_id,
                "gradeBookPK": grade_book_pk,
                "title": quote(title, safe=""),
                "sortDir": "ASCENDING",
                "editPaging": "false",
                "showAll": "true",
                "startIndex": 0,
            },
        )
        resp.raise_for_status()
        students = _parse_student_list(resp.text)
        if self.whitelist:
            students = [s for s in students if s["userId"] in self.whitelist]
        total = len(students)
        graded = sum(1 for s in students if s.get("already_graded"))
        return {"total": total, "graded": graded, "ungraded": total - graded}

    def fetch_due_date(self, grade_book_pk: str, title: str = "") -> str:
        """Fetch assignment due date.

        Tries Blackboard REST API first, then falls back to parsing
        the getStudentWork.do HTML page (which we already visit).

        Returns ISO 8601 string (e.g. '2026-03-22T15:59:00.000Z') or empty string.
        """
        # --- Attempt 1: Blackboard REST API ---
        col_id = f"_{grade_book_pk}_1"
        try:
            resp = self.client.get(
                f"{BB_API}/courses/{self.course_id}/gradebook/columns/{col_id}"
            )
            resp.raise_for_status()
            data = resp.json()
            for path in (
                lambda d: d.get("due", ""),
                lambda d: d.get("grading", {}).get("due", ""),
                lambda d: d.get("availability", {}).get("availability_dates", {}).get("due", ""),
            ):
                due = path(data)
                if due:
                    return due
        except Exception:
            pass

        # --- Attempt 2: Parse getStudentWork.do HTML ---
        try:
            safe_title = quote(title, safe="")
            resp = self.client.get(
                f"{HW_BASE}/getStudentWork.do",
                params={
                    "course_id": self.course_id,
                    "gradeBookPK": grade_book_pk,
                    "title": safe_title,
                    "showAll": "true",
                },
            )
            resp.raise_for_status()
            # Look for due date in various HTML patterns
            html = resp.text
            # Pattern 1: "截至时间" or "截止时间" followed by date
            m = re.search(r'截至时间[：:]\s*<span[^>]*>([\d\-:\s]+)</span>', html)
            if not m:
                m = re.search(r'截止时间[：:]\s*<span[^>]*>([\d\-:\s]+)</span>', html)
            if not m:
                # Pattern 2: "Due:" in English interface
                m = re.search(r'Due[：:]\s*<span[^>]*>([\d\-:\s]+)</span>', html, re.IGNORECASE)
            if m:
                raw = m.group(1).strip()
                # Convert "2026-04-15 23:59:00" → "2026-04-15T23:59:00"
                return raw.replace(" ", "T")
        except Exception:
            pass

        return ""

    def fetch_submissions(
        self,
        grade_book_pk: str,
        title: str,
        cache_dir: Path | None = None,
        verbose: bool = False,
        skip_ids: set[str] | None = None,
        limit: int = 0,
    ) -> list[Submission]:
        """
        Fetch student submissions for one assignment.

        Strategy:
        - Whitelist set -> per-student: CheckWork.do + api/pdf.do (2 reqs per student)
        - No whitelist  -> batch ZIP: downloadBatch.do (1 req for all files, fast)

        If *cache_dir* is provided, per-student mode will check for an existing
        local file before hitting the network.

        *skip_ids* — student IDs to skip (e.g. already graded in a previous run).
        *limit*    — maximum number of submissions to download (0 = no limit).
                      Only students who are NOT already graded online and NOT in
                      skip_ids count toward the limit.
        """
        self._ensure_bb_user_map()

        resp = self.client.get(
            f"{HW_BASE}/getStudentWork.do",
            params={
                "course_id": self.course_id,
                "gradeBookPK": grade_book_pk,
                "title": quote(title, safe=""),
                "sortDir": "ASCENDING",
                "editPaging": "false",
                "showAll": "true",
                "startIndex": 0,
            },
        )
        resp.raise_for_status()

        students = _parse_student_list(resp.text, verbose=verbose)

        if not students:
            return []

        if self.whitelist:
            return self._fetch_per_student(
                students, grade_book_pk, title,
                cache_dir=cache_dir, verbose=verbose,
                skip_ids=skip_ids, limit=limit,
            )
        else:
            return self._fetch_batch_zip(
                students, grade_book_pk, title,
                cache_dir=cache_dir, verbose=verbose,
                skip_ids=skip_ids, limit=limit,
            )

    # ------------------------------------------------------------------
    # Fetching strategies
    # ------------------------------------------------------------------

    def _fetch_per_student(
        self, students: list[dict], grade_book_pk: str, title: str,
        cache_dir: Path | None = None, verbose: bool = False,
        skip_ids: set[str] | None = None, limit: int = 0,
    ) -> list[Submission]:
        """Download individual files via CheckWork.do + api/pdf.do.

        If *cache_dir* is provided and a student's file already exists there,
        the local copy is used instead of hitting the network.
        """
        import re
        import sys

        submissions: list[Submission] = []
        skip_reasons: dict[str, int] = {
            "not_in_whitelist": 0,
            "in_skip_ids": 0,
            "already_graded": 0,
            "download_failed": 0,
            "no_file_found": 0,
            "limit_reached": 0,
        }

        for student in students:
            student_id = student["userId"]
            if student_id not in self.whitelist:
                skip_reasons["not_in_whitelist"] += 1
                continue
            if skip_ids and student_id in skip_ids:
                skip_reasons["in_skip_ids"] += 1
                continue
            if student.get("already_graded", False):
                skip_reasons["already_graded"] += 1
                continue
            if limit > 0 and len(submissions) >= limit:
                skip_reasons["limit_reached"] += 1
                if verbose:
                    print(f"  ! Reached download limit ({limit}), skipping remaining students")
                break

            # Try local cache first (skip if student has multiple attempts —
            # cached file may be an older version)
            file_bytes: bytes | None = None
            filename = ""
            content_type = ""
            has_multiple = student.get("has_multiple_attempts", False)
            if cache_dir is not None and not has_multiple:
                safe_name = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", student["name"])
                for ext in (".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".zip"):
                    candidate = cache_dir / f"{student_id}_{safe_name}{ext}"
                    if candidate.exists():
                        file_bytes = candidate.read_bytes()
                        filename = candidate.name
                        content_type = _guess_mime(filename)
                        break

            # Cache miss (or multiple attempts) → download from PKU
            if file_bytes is None:
                if verbose:
                    print(f"  → Downloading {student_id} {student['name']}...")
                try:
                    file_bytes, filename, content_type = self._download_student_file(
                        grade_book_pk=grade_book_pk,
                        title=title,
                        user_id=student_id,
                        file_pk=student["filePk"],
                        attempt_pk=student["attemptPk"],
                    )
                except Exception as e:
                    skip_reasons["download_failed"] += 1
                    if verbose:
                        print(f"  ✗ Download failed {student_id} {student['name']}: {e}")
                    continue
                if file_bytes is None:
                    skip_reasons["no_file_found"] += 1
                    if verbose:
                        print(f"  ! No file found {student_id} {student['name']}")
                    continue
                if verbose:
                    size_kb = len(file_bytes) / 1024
                    print(f"  ✓ Downloaded {student_id} {student['name']} — {filename} ({size_kb:.0f} KB)")

            submissions.append(Submission(
                student_id=student_id,
                student_name=student["name"],
                assignment_id=grade_book_pk,
                assignment_title=title,
                bb_user_id=self._bb_user_map.get(student_id, ""),
                attachments=[Attachment(filename=filename, content_type=content_type, data=file_bytes)],
                submitted_at=student.get("submitted_at", ""),
                already_graded=False,
                has_multiple_attempts=student.get("has_multiple_attempts", False),
            ))

        total_skipped = sum(skip_reasons.values())
        if total_skipped > 0:
            parts = [f"{k.replace('_', ' ')}={v}" for k, v in skip_reasons.items() if v > 0]
            print(
                f"  [fetch] {len(students)} students → {len(submissions)} downloaded, "
                f"{total_skipped} skipped ({', '.join(parts)})",
                file=sys.stderr,
            )

        return submissions

    def _fetch_batch_zip(
        self, students: list[dict], grade_book_pk: str, title: str,
        cache_dir: Path | None = None, verbose: bool = False,
        skip_ids: set[str] | None = None, limit: int = 0,
    ) -> list[Submission]:
        """Download all files at once via downloadBatch.do ZIP.

        Falls back to per-student fetching if batch download fails.
        When *cache_dir* is provided the fallback uses local cached files."""
        try:
            zip_resp = self.client.get(
                f"{HW_BASE}/downloadBatch.do",
                params={
                    "course_id": self.course_id,
                    "gradeBookPK": grade_book_pk,
                    "title": quote(title, safe=""),
                    "isGroup": "false",
                },
            )
            # Generic status check — works with both requests and httpx clients
            status = getattr(zip_resp, "status_code", getattr(zip_resp, "status", 0))
            if status != 200:
                # Log the actual URL for debugging
                actual_url = getattr(zip_resp, "url", "unknown")
                raise RuntimeError(f"Batch download returned HTTP {status} (URL: {actual_url})")

            with zipfile.ZipFile(io.BytesIO(zip_resp.content)) as zf:
                zip_files = [(name, zf.read(name)) for name in zf.namelist()]

            submissions: list[Submission] = []
            for student, (filename, file_bytes) in zip(students, zip_files):
                student_id = student["userId"]
                if skip_ids and student_id in skip_ids:
                    continue
                if student.get("already_graded", False):
                    continue
                if limit > 0 and len(submissions) >= limit:
                    break
                submissions.append(Submission(
                    student_id=student_id,
                    student_name=student["name"],
                    assignment_id=grade_book_pk,
                    assignment_title=title,
                    bb_user_id=self._bb_user_map.get(student_id, ""),
                    attachments=[Attachment(
                        filename=filename,
                        content_type=_guess_mime(filename),
                        data=file_bytes,
                    )],
                    submitted_at=student.get("submitted_at", ""),
                    already_graded=False,
                ))
            return submissions
        except (RuntimeError, zipfile.BadZipFile, Exception) as e:
            # Fall back to per-student fetching if batch download fails
            import sys
            print(f"  Batch download failed (will fetch individually): {e}", file=sys.stderr)
            # Temporarily set whitelist to all students to trigger per-student fetch
            original_whitelist = self.whitelist
            self.whitelist = {s["userId"] for s in students}
            try:
                return self._fetch_per_student(
                    students, grade_book_pk, title,
                    cache_dir=cache_dir, verbose=verbose,
                    skip_ids=skip_ids, limit=limit,
                )
            finally:
                self.whitelist = original_whitelist

    def _download_student_file(
        self, grade_book_pk: str, title: str, user_id: str, file_pk: str, attempt_pk: str
    ) -> tuple[bytes | None, str, str]:
        """
        Fetch CheckWork.do to extract filePath, then download via api/pdf.do.
        Returns (file_bytes, filename, content_type) or (None, "", "") on failure.
        """
        # Step 1: fetch CheckWork.do to get the JS variable filePath
        url = f"{self.client.base_url or ''}{HW_BASE}/CheckWork.do"
        params = {
            "course_id": self.course_id,
            "gradeBookPK": grade_book_pk,
            "userId": user_id,
            "filePk": file_pk,
            "title": title,
            "attemptPk": attempt_pk,
        }
        resp = self.client.get(url, params=params)
        resp.raise_for_status()

        m = _FILE_PATH_PATTERN.search(resp.text)
        if not m:
            return None, "", ""

        file_path = m.group(1)
        filename = file_path.rsplit("/", 1)[-1]
        content_type = _guess_mime(filename)

        # Double-encode and pass directly in URL (params= would triple-encode)
        encoded = quote(quote(file_path, safe=""), safe="")
        file_resp = self.client.get(f"{HW_BASE}/api/pdf.do?path={encoded}")
        file_resp.raise_for_status()

        return file_resp.content, filename, content_type

    # ------------------------------------------------------------------
    # BB user map (for grade submission)
    # ------------------------------------------------------------------

    def _ensure_bb_user_map(self) -> None:
        if self._bb_user_map:
            return
        try:
            users = self._bb_paginate(
                f"{BB_API}/courses/{self.course_id}/users",
                params={"fields": "userId,user.userName"},
            )
            for u in users:
                bb_uid = u.get("userId", "")
                student_number = u.get("user", {}).get("userName", "")
                if bb_uid and student_number:
                    self._bb_user_map[student_number] = bb_uid
        except httpx.HTTPStatusError:
            pass  # non-fatal

    def _bb_paginate(self, path: str, params: dict | None = None) -> list[dict]:
        params = dict(params or {})
        params.setdefault("limit", "200")
        results: list[dict] = []
        url: str | None = path
        while url:
            resp = self.client.get(url, params=params if url == path else None)
            resp.raise_for_status()
            body = resp.json()
            results.extend(body.get("results", []))
            url = body.get("paging", {}).get("nextPage")
        return results


# ------------------------------------------------------------------
# HTML parsers
# ------------------------------------------------------------------

def _parse_homework_list(html: str) -> list[dict]:
    """Extract assignment list from getHomeWorkList.do HTML."""
    pattern = re.compile(
        r'getStudentWork\.do\?[^"\']*?title=([^&"\']+)[^"\']*?gradeBookPK=(\d+)|'
        r'getStudentWork\.do\?[^"\']*?gradeBookPK=(\d+)[^"\']*?title=([^&"\']+)'
    )
    seen: set[str] = set()
    assignments: list[dict] = []
    for m in pattern.finditer(html):
        title = unquote(m.group(1) or m.group(4) or "")
        pk = m.group(2) or m.group(3) or ""
        if pk and pk not in seen:
            seen.add(pk)
            assignments.append({"id": f"_{pk}_1", "name": title, "gradeBookPK": pk})
    return assignments


def _parse_student_list(html: str, verbose: bool = False) -> list[dict]:
    """Extract submitted student list from getStudentWork.do HTML.

    Handles three link formats:
    - CheckAloneWork.do href with "查看" (view, already graded)
    - CheckWork.do href with "批改" (grade, needs grading)
    - onclick="checkWork('userId','filePk','attemptPk',...)" with "批改" (grade, needs grading)

    For students with multiple attempts, keeps the newest one (highest attemptPk).
    Adds an 'already_graded' field to indicate if the attempt is already graded.
    """
    import sys

    names = {m.group(1): m.group(2).strip() for m in _NAME_PATTERN.finditer(html)}
    submitted_ats = {m.group(1): m.group(2).strip() for m in _SUBMITTED_AT_PATTERN.finditer(html)}
    student_map: dict[str, dict] = {}  # userId -> best attempt
    all_attempts: dict[str, list[dict]] = {}  # userId -> all attempts found (for logging)

    def _keep_best(user_id: str, attempt: dict, source: str) -> None:
        if user_id not in all_attempts:
            all_attempts[user_id] = []
        all_attempts[user_id].append({**attempt, "source": source})
        if user_id not in student_map or int(attempt["attemptPk"]) > int(student_map[user_id]["attemptPk"]):
            student_map[user_id] = attempt

    for m in _STUDENT_PATTERN.finditer(html):
        groups = m.groups()
        if len(groups) == 6:
            _, user_id, file_pk, title_enc, attempt_pk, link_text = groups
        else:
            continue
        already_graded = link_text.strip() == "查看"
        _keep_best(user_id, {
            "userId": user_id,
            "filePk": file_pk,
            "attemptPk": attempt_pk,
            "name": names.get(user_id, "Unknown"),
            "already_graded": already_graded,
            "submitted_at": submitted_ats.get(user_id, ""),
        }, source="href")

    for m in _STUDENT_ONCLICK_PATTERN.finditer(html):
        groups = m.groups()
        if len(groups) == 4:
            user_id, file_pk, attempt_pk, link_text = groups
        else:
            continue
        already_graded = link_text.strip() == "查看"
        _keep_best(user_id, {
            "userId": user_id,
            "filePk": file_pk,
            "attemptPk": attempt_pk,
            "name": names.get(user_id, "Unknown"),
            "already_graded": already_graded,
            "submitted_at": submitted_ats.get(user_id, ""),
        }, source="onclick")

    # Mark students with multiple attempts so caller can skip stale cache
    for user_id, attempt in student_map.items():
        attempt["has_multiple_attempts"] = len(all_attempts.get(user_id, [])) > 1

    if verbose:
        for user_id, attempts in sorted(all_attempts.items()):
            if len(attempts) > 1:
                selected = student_map[user_id]
                lines = ", ".join(
                    f"#{a['attemptPk']} {a['source']}({a['already_graded'] and '已批' or '未批'})"
                    for a in attempts
                )
                print(f"  [attempts] {user_id}: found {len(attempts)} → [{lines}] → selected #{selected['attemptPk']}", file=sys.stderr)

    return list(student_map.values())


def _guess_mime(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "pdf": "application/pdf",
        "doc": "application/msword",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "zip": "application/zip",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
    }.get(ext, "application/octet-stream")
