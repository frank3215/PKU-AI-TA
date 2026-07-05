# TUI Review Guide

The interactive TUI (Terminal User Interface) is the recommended way to review and approve scores. It displays each student's breakdown side by side with tools for editing notes, overriding scores, and searching past reviews.

## Starting the TUI

```bash
# Review only students flagged for review (default)
uv run python main.py review --needs-review

# Review all students
uv run python main.py review --all

# Review a specific scores file
uv run python main.py review --scores scores_426252.xlsx --needs-review

# Auto-approve perfect scores first, then review the rest
uv run python main.py review --auto-approve --needs-review
```

## Command Overview

| Key | Action | Stays on page |
|---|---|---|
| `a` | Approve and advance to next student | No |
| `n` | Add / edit reviewer notes — **write deductions here** | Yes |
| `o` | Open submission file with system viewer | Yes |
| `r` | Open rubric file | Yes |
| `u` | Toggle uncertain parts auto-append to notes | Yes |
| `R` | Recall — search approved students' notes | Yes |
| `j` | Jump to student by name or ID | No |
| `s` | Skip (do not approve), advance to next | No |
| `b` | Go back to previous student | No |
| `q` | Quit | — |

## Core Workflow: Notes-based Override

The preferred way to adjust scores is through **reviewer notes** (`n`). This replaces the now-hidden `e` (edit) and `ov` (override) commands.

### How it works

1. Press `n` to open the system editor.
2. Write your assessment. To deduct points, use the format:

   ```
   扣X分：原因说明
   ```

   For example:

   ```
   扣3分：7.7的等价性证明只给出了结论性语句，没有展开说明双向对应关系。
   扣2分：7.21(1)中库存边费用写成0.5，题目为0.1。
   ```

3. Save and close the editor. The system automatically:
   - Parses all `扣X分` patterns from your notes
   - Computes the suggested total score (总分 - 扣分之和)
   - Prompts you to confirm an override

4. Confirm the override — the `reviewer_override_score` is set automatically.

### Why notes instead of edit/override

- **Audit trail**: the reason for each deduction is captured in the notes
- **Consistency**: when you search with `R(ecall)` later, the rationale is visible
- **Accuracy**: manual `e`(dit) requires adjusting raw breakdown scores, which is error-prone

## Uncertain Parts

If the LLM flagged uncertain parts (borderline scores, illegible handwriting), they are displayed under a yellow "Uncertain Parts" panel during review.

Press `u` to toggle **auto-append** of uncertain parts to the notes on first edit. When enabled, the first `n` for each student pre-populates the notes with a summary:

```
--- Uncertain Parts ---
- 字迹不清，无法判断第2题具体写了什么 (suggested: 0/10)
- borderline: 可能在7分和8分之间 (suggested: 8/10)
```

You can decide whether to flag this for the grader, adjust your score accordingly, or ignore it.

## Early Submission Detection

The system automatically checks submitted_at against the due date using **Beijing time (UTC+8)**. Students who submitted before the deadline are correctly marked as on time. Students who submitted after are flagged as late with a 10-point penalty (capped at total_score).

The late info is prepopulated into notes on the first `n` edit (and tracked per-student to avoid re-appearing if you delete it).

## Recall: Reviewing past decisions

When reviewing similar issues across students, use `R(ecall)` to search previously approved notes:

```
Action: R
Recall: search notes for: 7.4
```

Output format:
```
2400017436_潘玥含: 题目7.4...入度...扣2分
2400017451_解成毅: 7.4...核心证明缺失...扣7分
```

This helps maintain **grading consistency** — you can verify that similar mistakes get similar deductions across different students.

## Jump: Navigating to specific students

If `Recall` reveals a potential inconsistency in a previous student's score, use `j(ump)` to navigate directly to them:

```
Action: j
Jump to student (name or ID): 解成
```

The TUI jumps to the matching student. You can then review their breakdown, adjust notes, or approve.

## Late Submission Notes

When a student submitted late:
- The first `n` edit prepopulates the late notice: `晚交 X 天，扣 10 分。`
- You can keep, edit, or delete this line
- Once removed, it won't be re-added (tracked per-student)
- Use `recheck-late` CLI command to fix late calculations if the timezone was wrong

## Full Review Session Flow

```
1. Start TUI:  python main.py review --needs-review
2. For each student:
   a. Read the breakdown and LLM reasoning
   b. Open the submission (o) and rubric (r) as needed
   c. Write notes with deductions in 扣X分 format (n / T)
   d. Confirm the auto-suggested override if applicable
   e. Approve (a) to move to the next student
   f. If uncertain, skip (s) and come back later
3. Save when prompted on exit
```
