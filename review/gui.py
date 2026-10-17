"""
Minimal local HTTP server for reviewing scores in a browser.
Serves a single HTML page with PDF side-by-side with notes editor.

Usage:
    uv run python -m review.gui --scores scores.xlsx --port 8080
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

from review.tui_components import ReviewSession, find_submission_file, _suggest_override_from_notes

HERE = Path(__file__).parent
HTML_PATH = HERE / "gui.html"


class ReviewGUIHandler(BaseHTTPRequestHandler):
    """HTTP handler for the review GUI. Set class attributes before starting."""

    session: ReviewSession = None
    submissions_dir: Path = Path("submissions")

    # ── Routes ────────────────────────────────────────────────

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "" or path == "/index.html":
            self._serve_html()
        elif path == "/api/students":
            self._serve_students()
        elif path.startswith("/api/student/"):
            parts = path.split("/")
            if len(parts) >= 4 and parts[3].isdigit():
                self._serve_student(int(parts[3]))
            else:
                self.send_error(400)
        elif path.startswith("/api/pdf/"):
            parts = path.split("/")
            if len(parts) >= 4 and parts[3].isdigit():
                self._serve_pdf(int(parts[3]))
            else:
                self.send_error(400)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/student/") and path.endswith("/save"):
            parts = path.split("/")
            if len(parts) >= 4 and parts[3].isdigit():
                self._save_student(int(parts[3]))
            else:
                self.send_error(400)
        else:
            self.send_error(404)

    # ── Handlers ──────────────────────────────────────────────

    def _serve_html(self):
        if HTML_PATH.exists():
            html = HTML_PATH.read_text(encoding="utf-8")
            self._send_text(html, "text/html; charset=utf-8")
        else:
            self.send_error(500, f"gui.html not found at {HTML_PATH}")

    def _serve_students(self):
        rows = self.session.rows
        out = []
        for i, (row_idx, row_data) in enumerate(rows):
            out.append({
                "idx": i,
                "student_id": row_data.get("student_id", ""),
                "student_name": row_data.get("student_name", ""),
                "total_score": row_data.get("total_score", 0),
                "total_max": row_data.get("total_max", 100),
                "approved": str(row_data.get("approved", "")).upper() == "YES",
                "needs_review": row_data.get("needs_review") == "YES",
            })
        self._send_json(out)

    def _serve_student(self, idx: int):
        rows = self.session.rows
        if idx < 0 or idx >= len(rows):
            self.send_error(404, "Student not found")
            return
        row_idx, row_data = rows[idx]

        # Parse JSON fields
        breakdown = []
        try:
            breakdown = json.loads(row_data.get("breakdown_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            pass

        uncertain = []
        try:
            uncertain = json.loads(row_data.get("uncertain_parts_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            pass

        # Compute suggested override from notes
        notes = str(row_data.get("reviewer_notes") or "")
        total_max = float(row_data.get("total_max", 100) or 100)
        suggested = _suggest_override_from_notes(notes, total_max)

        current_override = row_data.get("reviewer_override_score")
        current_override = float(current_override) if current_override not in (None, "") else None

        out = {
            "idx": idx,
            "total": len(rows),
            "student_id": row_data.get("student_id", ""),
            "student_name": row_data.get("student_name", ""),
            "total_score": row_data.get("total_score", 0),
            "total_max": total_max,
            "pct": round(float(row_data.get("total_score", 0) or 0) / total_max * 100, 1) if total_max else 0,
            "confidence": row_data.get("confidence", 1.0),
            "needs_review": row_data.get("needs_review") == "YES",
            "late_days": round(float(row_data.get("late_days") or 0), 2),
            "late_penalty": float(row_data.get("late_penalty") or 0),
            "breakdown": breakdown,
            "uncertain_parts": uncertain,
            "llm_reasoning": row_data.get("llm_reasoning", ""),
            "processing_notes": row_data.get("processing_notes", ""),
            "reviewer_notes": notes,
            "reviewer_override_score": current_override,
            "approved": str(row_data.get("approved", "")).upper() == "YES",
            "suggested_override": round(suggested, 1) if suggested is not None else None,
        }
        self._send_json(out)

    def _serve_pdf(self, idx: int):
        rows = self.session.rows
        if idx < 0 or idx >= len(rows):
            self.send_error(404)
            return
        row_idx, row_data = rows[idx]
        sid = str(row_data.get("student_id", ""))
        sname = str(row_data.get("student_name", ""))
        aid = self.session.assignment_id or ""

        pdf_path = find_submission_file(self.submissions_dir, sid, sname, assignment_id=aid)
        if not pdf_path or not Path(pdf_path).exists():
            self.send_error(404, "No PDF submission file found")
            return

        pdf_path = Path(pdf_path)
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(pdf_path.stat().st_size))
        # Use ASCII-safe filename to avoid latin-1 encoding errors in HTTP headers
        safe_name = Path(pdf_path).name.encode("ascii", "replace").decode("ascii")
        self.send_header("Content-Disposition", f'inline; filename="{safe_name}"')
        self.end_headers()
        with open(pdf_path, "rb") as f:
            self.wfile.write(f.read())

    def _save_student(self, idx: int):
        rows = self.session.rows
        if idx < 0 or idx >= len(rows):
            self.send_error(404)
            return
        row_idx, row_data = rows[idx]

        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len)
        data = json.loads(body) if body else {}

        # Update notes
        notes = data.get("reviewer_notes")
        if notes is not None:
            row_data["reviewer_notes"] = notes

        # Update approved
        approved = data.get("approved")
        if approved is not None:
            row_data["approved"] = "YES" if approved else "NO"

        # Auto-compute override from notes if applicable
        total_max = float(row_data.get("total_max", 100) or 100)
        suggested = _suggest_override_from_notes(str(row_data.get("reviewer_notes", "")), total_max)
        if suggested is not None:
            row_data["reviewer_override_score"] = suggested
        else:
            # If notes have no deductions, clear override if it was auto-set
            current_notes = str(row_data.get("reviewer_notes", ""))
            # Only clear override if user explicitly asks (via JSON body)
            clear_override = data.get("clear_override")
            if clear_override:
                row_data["reviewer_override_score"] = None

        self.session.update_row(row_idx, row_data)
        self.session.save_changes()

        # Return updated student data
        self._serve_student(idx)

    # ── Helpers ───────────────────────────────────────────────

    def _send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_text(body, "application/json; charset=utf-8")

    def _send_text(self, body: str | bytes, content_type: str):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        if "/api/" in str(args[0]) if len(args) > 0 else True:
            super().log_message(format, *args)


def run_server(
    scores: Path,
    submissions_dir: Path,
    needs_review_only: bool,
    all_students: bool,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> None:
    """Start the review GUI server."""
    session = ReviewSession(scores, needs_review_only, all_students)
    if not session.rows:
        print("No students to review.")
        return

    ReviewGUIHandler.session = session
    ReviewGUIHandler.submissions_dir = submissions_dir

    server = HTTPServer((host, port), ReviewGUIHandler)
    url = f"http://{host}:{port}"
    print(f"\n  Review GUI: {url}")
    print(f"  Students: {len(session.rows)}")
    print("  Press Ctrl-C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--scores", default="scores.xlsx", type=Path)
    parser.add_argument("--submissions", default="submissions", type=Path)
    parser.add_argument("--needs-review", action="store_true")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    run_server(args.scores, args.submissions, args.needs_review, args.all, args.host, args.port)
