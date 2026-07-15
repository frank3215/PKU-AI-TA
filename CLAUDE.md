# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PKU AI Teaching Assistant — a Python CLI that auto-grades student homework submissions from PKU's Blackboard Learn instance (course.pku.edu.cn) using an LLM, exports results to Excel for human review, and submits approved scores back.

**Workflow:** `uv run python main.py grade` → `uv run python main.py review` → `uv run python main.py submit`

## Development Commands

| Task | Command |
|---|---|
| Install deps | `uv sync` |
| Install with dev deps | `uv sync --extra dev` |
| Run tests | `uv run pytest tests/ -v` |
| Run single test | `uv run pytest tests/test_scorer_llm.py -v` |
| Run CLI | `uv run python main.py <command>` |

The project uses `uv` as the package manager. Do not use `pip` directly.

## Architecture

### CLI Commands (`main.py`)

Six Typer commands orchestrate the workflow:

- **`grade`** — Crawl submissions, score with LLM in parallel, export `scores.xlsx`. Supports `--resume`, `--regrade-unapproved`, `--threads`, `--limit N` (grade only first N submissions), `--due-date`, `--auto-submit`, `--student-list`, `--interactive`. Checkpoint is saved after each student.
- **`review`** — Interactive TUI for one-by-one review. Keys: `a` approve, `e` edit criterion scores, `n` notes, `ov` override, `o` open file, `r` rubric, `s` skip, `b` back, `u` toggle uncertain auto-append, `R` recall/search notes, `j` jump to student, `q` quit. Supports `--skip-submitted` (skip students already uploaded to Blackboard), `--needs-review-only`, `--auto-approve`, `--auto-skip-late`.
- **`submit`** — Push approved grades back to Blackboard. Shows a diff table (local vs online) before confirming upload. Supports `--dry-run`, `--encourage`.
- **`list`** — List assignments with gradeBookPK and submission/grading counts.
- **`recheck-late`** — Re-check late penalties on an existing `scores.xlsx` after the fact. Supports `--late-cutoff YYYY-MM-DD` for a manual cutoff date override.
- **`review-gui`** — Launch a browser-based GUI review interface (served locally via `gui.html`).
- **`batch`** — Grade in batched passes: grade the first N submissions, review, save, then grade the next N. Useful for managing LLM context / cost on large assignments.

### Data Models (`models.py`)

Core Pydantic models:

- `Submission` — student submission with attachments, text content, and metadata.
- `ScoringResult` — LLM output: breakdown, uncertain parts, confidence, reasoning.
- `ReviewRecord` — wraps `ScoringResult` with human review fields (`reviewer_override_score`, `reviewer_notes`, `approved`).

### Authentication (`auth/iaaa.py`)

Reverse-engineered PKU IAAA SSO flow:

1. Fetch RSA public key from IAAA.
2. Encrypt password with PKCS1v15.
3. POST to `oauthlogin.do` for a token.
4. Exchange token for Blackboard session via `campusLogin`.

**Critical:** Uses `requests` (not `httpx`) because it handles multiple `Set-Cookie` headers with the same name without raising `CookieConflict`. IAAA's `JSESSIONID` must never be sent to Blackboard — it causes stale/default responses. The implementation collects only Blackboard cookies (`s_session_id`) and injects them manually via a `Cookie:` header on every request. PKU's CA is not in the default trust store, so `verify=False` is required.

### Crawling (`crawler/`)

PKU uses a custom homework plugin (`bb-homeWorkCheck-BBLEARN`), not standard Blackboard REST APIs for submission files.

- **`pku_homework.py`** — The active crawler. Scrapes HTML from:
  - `getHomeWorkList.do` — assignment list with `gradeBookPK`.
  - `getStudentWork.do` — student list with `userId`, `filePk`, `attemptPk`.
  - `CheckWork.do` — per-student page containing a JS `filePath` variable.
  - `api/pdf.do?path=` — download the actual file. The path is **double-URL-encoded** in the query string; using `params=` would triple-encode it, so the URL is constructed manually.
  - `downloadBatch.do` — bulk ZIP download (used when no whitelist is set).

  When a whitelist is set, fetches per-student and checks local cache (`submissions/`) before hitting the network. Multiple attempts are tracked; only the newest (highest `attemptPk`) is kept.

- **`blackboard.py`** — Blackboard REST API crawler. Present but largely unused since PKU's custom plugin does not expose submission files through REST.

### Scoring (`scorer/llm.py`)

Scores one submission at a time. Supports both OpenAI-compatible and Anthropic-native APIs.

**Attachment processing pipeline:**
1. Extract text: `pypdf` for PDFs, `python-docx` for Word, raw bytes for images.
2. Detect if vision is needed: unreadable markers, embedded images in PDF, very short text (<200 chars), low CJK ratio (garbled OCR), or garbled text patterns.
3. Fallback to vision: render PDF pages to PNG at 2× scale via `pymupdf`, or send JPEG/PNG directly as base64.
4. Send to LLM with system prompt + rubric + submission content.
5. Parse JSON response with progressively aggressive fixes (markdown fences, newlines in strings, stray backslashes).
6. Validate consistency: `total_score` must equal sum of `breakdown` points; corrected if mismatched.

**Provider switching:** controlled by `LLM_PROVIDER` env var (`openai` or `anthropic`). Anthropic format conversion is handled in `_to_anthropic_content()`.

System prompts live in `prompts/` (Chinese default `system_zh.md`, English `system_en.md`). They are plain Markdown and define the JSON output schema, grading philosophy, and student feedback tone.

Text attachments are parsed via `scorer/parsers/text.py`; the subpackage can be extended with additional format parsers.

### Review (`review/`)

- **`spreadsheet.py`** — Export/import `scores.xlsx`. Writes atomically (temp file + rename). Rows needing review are highlighted yellow. JSON fields (breakdown, uncertain parts) are serialized as strings in cells.
- **`tui.py`** — Interactive review loop using Rich. Displays student info, score breakdown, uncertain parts, and LLM reasoning.
- **`tui_components.py`** — Session state management, file discovery, cross-platform file opening, and input helpers. Uses `input()` (not Rich prompts) for better IME/CJK support.

### Submission (`submitter/blackboard.py`)

Posts grades to PKU's custom plugin via `saveStudentGrade.do`:

- Fetches `CheckWork.do` per student to extract `gradePk` (a per-student grade record ID injected in page JS).
- Fetches existing online grades to build a diff table before upload.
- Skips unapproved records.
- Supports `--dry-run` and `--encourage` (message attached to perfect scores with no notes).

### Utilities

- **`iterate.py`** — Standalone script to quickly re-score a single student after rubric/prompt changes. Loads from cache or fetches online, scores, and diffs against existing `scores.xlsx` entry.
- **`config.py`** — Pydantic-settings loaded from `.env`. Key fields: `llm_provider`, `openai_base_url`, `openai_api_key`, `ta_model`, `enable_thinking`, `pku_username`, `pku_password`, `course_id`, `student_whitelist`, `review_threshold`, `ta_threads`, `user_agent`, `fallback_model` (model to retry with on timeout), `llm_timeout` (seconds, 0 = no timeout), `auto_submit`, `append_uncertain_to_notes`, `editor` (default editor for multi-line input), `dashscope_api_key` (for qwen benchmark).

## Important Conventions

- **Do not use `httpx` for auth** — the cookie conflict behavior breaks IAAA/Blackboard handoff.
- **Do not trust `params=` for `api/pdf.do`** — double-encode the path manually in the URL string.
- **Excel is the checkpoint format** — `scores.xlsx` is both the output and the resume source. The temp-file + `os.replace` pattern prevents corruption on interrupt.
- **Prompts are externalized** — modify `prompts/*.md` to change grading behavior without touching code.
- **Tests are pytest-based** — mock-heavy unit tests for JSON parsing, text extraction, and spreadsheet round-trips. No online integration tests.
- **Set `llm_timeout` for models prone to thinking loops** — MiniMax-M3 can loop indefinitely; `llm_timeout=120` (or similar) prevents runaway costs. The `fallback_model` env var provides a retry target if the primary model times out.
