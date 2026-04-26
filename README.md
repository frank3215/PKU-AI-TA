# PKU AI Teaching Assistant

Automatically grades student homework submissions from [course.pku.edu.cn](https://course.pku.edu.cn) (Blackboard Learn) using an LLM, exports results to Excel for human review, and submits approved scores back to the platform.

**Workflow:** crawl submissions → LLM scores against rubric → review (TUI or Excel) → submit scores

---

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- An LLM API key (OpenAI-compatible or Anthropic-native endpoint)
- PKU IAAA credentials (student/staff ID + password)

---

## Setup

**1. Clone and install**

```bash
git clone <repo-url>
cd PKU-AI-TA
uv sync
```

**2. Configure credentials**

```bash
cp .env.example .env
# Edit .env with your credentials
```

Key variables in `.env`:

| Variable | Description |
|---|---|
| `LLM_PROVIDER` | `openai` (OpenAI-compatible) or `anthropic` (native Messages API) |
| `OPENAI_API_KEY` | Your API key |
| `OPENAI_BASE_URL` | API endpoint (default: `https://openrouter.ai/api/v1`) |
| `TA_MODEL` | Model to use, e.g. `qwen/qwen3.5-397b-a17b` |
| `PKU_USERNAME` | Your PKU student/staff ID |
| `PKU_PASSWORD` | Your PKU password |
| `COURSE_ID` | Blackboard course ID, e.g. `_98024_1` (from the course URL) |
| `REVIEW_THRESHOLD` | Confidence below this → flagged yellow for review (default: `0.75`) |

**3. Prepare your rubric**

Create `rubric.md` describing the scoring criteria. Example:

```markdown
# Homework 1 Rubric (100 points)

## Problem 1.2 (12 pts)
- Part 1 (6 pts): correct answer = full marks
- Part 2 (6 pts): correct answer = full marks; only worst-case = 3 pts

## Problem 1.6 (12 pts)
- Correct algorithm (4 pts)
- Correct multiplication count (4 pts)
- Correct addition count (4 pts)
```

**4. Prepare your student list** (optional — for targeting specific students)

```
# student_list  (one ID per line)
2300012345
2300012346
2300012347
```

---

## Usage

### Step 1 — Grade

Crawl submissions, score with LLM, and export a review spreadsheet:

```bash
# Grade all students in the course
uv run python main.py grade --course _98024_1 --column 423829 --rubric rubric.md

# Grade only students in your whitelist file
uv run python main.py grade \
  --course _98024_1 \
  --column 423829 \
  --rubric rubric.md \
  --whitelist $(cat student_list | tr '\n' ',' | sed 's/,$//') \
  --out scores.xlsx

# Save submission files to a custom directory for review
uv run python main.py grade --course _98024_1 --column 423829 --rubric rubric.md \
  --save-dir ./hw1_submissions

# Use the English system prompt
uv run python main.py grade --course _98024_1 --column 423829 --rubric rubric.md \
  --prompt prompts/system_en.md

# Interrupt with Ctrl-C and resume later
uv run python main.py grade --course _98024_1 --column 423829 --rubric rubric.md --resume

# Keep already-approved students, regrade the rest
uv run python main.py grade --course _98024_1 --column 423829 --rubric rubric.md \
  --regrade-unapproved

# Control parallel scoring threads (default is 4; use 1 to disable parallelism)
uv run python main.py grade --course _98024_1 --column 423829 --rubric rubric.md \
  --threads 2
```

| Flag | Description |
|---|---|
| `--course` | Blackboard course ID (or set `COURSE_ID` in `.env`) |
| `--column` | Assignment `gradeBookPK` — the numeric ID in the `getStudentWork.do` URL |
| `--rubric` | Path to your rubric Markdown file |
| `--whitelist` | Comma-separated student IDs to grade; omit to grade everyone |
| `--out` | Output Excel file (default: `scores.xlsx`) |
| `--save-dir` | Directory to save submission files for human review (default: `submissions/`) |
| `--prompt` | System prompt file for the LLM (default: `prompts/system_zh.md`) |
| `--verbose` / `-v` | Print each student's result as it's scored |
| `--resume` / `-r` | Resume a previously interrupted run |
| `--regrade-unapproved` | Keep approved students, regrade the rest |
| `--threads` | Number of parallel scoring threads (default: from `.env` or 4) |

> **Using Anthropic-compatible endpoint:**
> ```bash
> # .env
> LLM_PROVIDER=anthropic
> OPENAI_BASE_URL=https://your-provider.com/v1
> OPENAI_API_KEY=sk-xxx
> TA_MODEL=your-model-name
> ```

This produces `scores.xlsx`. Rows highlighted **yellow** have low LLM confidence and need manual review.

> **Finding `--course` (course ID):**
> Navigate to any page of your course on course.pku.edu.cn. The URL contains `course_id=_98024_1` — copy that value including the underscores.
>
> **Finding `--column` (assignment ID):**
> Go to the homework list, click **查看** next to any assignment. The URL of the student list page looks like:
> ```
> …/getStudentWork.do?course_id=_98024_1&gradeBookPK=423829&title=第一次作业
> ```
> Copy the bare number after `gradeBookPK=`, e.g. `423829`.

### Step 2 — Review

#### Option A: Interactive TUI (recommended)

```bash
# Review only students flagged for review
uv run python main.py review --needs-review

# Review all students
uv run python main.py review --all

# Auto-approve perfect scores, then review the rest
uv run python main.py review --auto-approve --needs-review

# Review a specific spreadsheet (e.g., from a previous grading run)
uv run python main.py review --scores scores1.xlsx --needs-review
```

TUI key bindings:

| Key | Action | Stays on page |
|---|---|---|
| `a` | Approve and advance to next student | No |
| `e` | Edit individual criterion scores interactively | Yes |
| `n` | Add / edit reviewer notes | Yes |
| `ov` | Set override score | Yes |
| `o` | Open submission file with system viewer | Yes |
| `r` | Open rubric file | Yes |
| `s` | Skip (do not approve), advance to next | No |
| `b` | Go back to previous student | No |
| `q` | Quit | — |

| Flag | Description |
|---|---|
| `--scores` | Excel file to review (default: `scores.xlsx`) |
| `--submissions` | Directory with submission files (default: `submissions/`) |
| `--rubric` | Rubric file to open with `r` (default: `rubric.md`) |
| `--needs-review` / `-n` | Only show students flagged as needing review |
| `--all` / `-a` | Show all students, including already-approved |
| `--auto-approve` | Automatically approve 100/100 students with no uncertain parts |

#### Option B: Edit Excel directly

Open `scores.xlsx`. For each student:

1. Check `breakdown_json` and `llm_reasoning`.
2. Optionally set `reviewer_override_score`.
3. Add notes in `reviewer_notes`.
4. Set `approved` to **YES**.

Rows without `approved = YES` are never submitted.

### Step 3 — Submit

Push approved scores back to Blackboard:

```bash
uv run python main.py submit \
  --course _98024_1 \
  --column 423829 \
  --scores scores.xlsx

# Preview without posting
uv run python main.py submit --course _98024_1 --column 423829 --scores scores.xlsx --dry-run
```

---

## Customising the system prompt

Built-in prompts live in `prompts/`:

| File | Language |
|---|---|
| `prompts/system_zh.md` | Chinese (default) |
| `prompts/system_en.md` | English |

Pass any prompt file to `--prompt`:

```bash
uv run python main.py grade ... --prompt prompts/system_en.md
uv run python main.py grade ... --prompt my_custom_prompt.md
```

The prompt files are plain Markdown — edit them directly to adjust grading philosophy, tone, or output format without touching code.

---

## How submissions are handled

| File type | How it's processed |
|---|---|
| Text-embedded PDF | Text extracted with `pypdf`, sent to LLM as text |
| Scanned / image PDF | Pages rendered at 2× scale via `pymupdf`, sent as images |
| Submitted JPEG / PNG | Sent directly as images |
| Word (.docx) | Text extracted with `python-docx` |

The LLM is instructed to give students the benefit of the doubt: every deduction must have an explicit reason and point amount. Uncertain parts are flagged for human review rather than penalised.

---

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run tests
uv run pytest tests/ -v
```
