"""
Benchmark two LLM models on the same student submissions for side-by-side comparison.

Usage:
    uv run python benchmark_models.py \
        --assignment-dir "submissions/第一次作业" \
        --rubric rubric.md \
        --prompt prompts/system_zh.md \
        --count 10

Model A uses the current .env settings (kimi).
Model B is hard-coded to qwen3.5-plus via dashscope.

Output: a rich table showing score diff, breakdown diff, and flag disagreements.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from time import time

from config import settings
from models import Attachment, CriterionScore, ScoringResult, Submission
from rich.console import Console
from rich.table import Table

console = Console()

# ---------------------------------------------------------------------------
# Model B config (qwen via dashscope)
# ---------------------------------------------------------------------------
QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_MODEL = "qwen3.5-plus-2026-02-15"

# ---------------------------------------------------------------------------
# Load submissions from cache
# ---------------------------------------------------------------------------

def _parse_filename(filename: str) -> tuple[str, str] | None:
    """Parse '2100012151_王喆楷.pdf' -> (student_id, student_name)."""
    m = re.match(r"^(\d+)_(.+)\.(.+)$", filename)
    if not m:
        return None
    return m.group(1), m.group(2)


def _read_env_key(key: str) -> str:
    """Read a key directly from .env file, bypassing env var override."""
    env_path = Path(".env")
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return ""


def load_cached_submissions(assignment_dir: Path, count: int) -> list[Submission]:
    """Load up to *count* submissions from the cache directory."""
    files = sorted([f for f in assignment_dir.iterdir() if f.is_file() and not f.name.startswith(".")])
    submissions: list[Submission] = []
    for f in files[:count]:
        parsed = _parse_filename(f.name)
        if not parsed:
            continue
        student_id, student_name = parsed
        data = f.read_bytes()
        ext = f.suffix.lower()
        mime = {
            ".pdf": "application/pdf",
            ".doc": "application/msword",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".md": "text/markdown",
            ".txt": "text/plain",
        }.get(ext, "application/octet-stream")

        submissions.append(
            Submission(
                student_id=student_id,
                student_name=student_name,
                assignment_id="",
                assignment_title=assignment_dir.name,
                bb_user_id="",
                attachments=[Attachment(filename=f.name, content_type=mime, data=data)],
                submitted_at="",
            )
        )
    return submissions


# ---------------------------------------------------------------------------
# Score with a temporarily switched model
# ---------------------------------------------------------------------------

def score_with_model(
    submissions: list[Submission],
    rubric_text: str,
    prompt: Path,
    base_url: str,
    api_key: str,
    model: str,
    label: str,
    provider: str = "openai",
) -> list[tuple[Submission, ScoringResult, float]]:
    """Score all submissions with a specific model config.

    Returns list of (submission, result, elapsed_seconds).
    """
    from scorer.llm import score_submission

    # Monkey-patch settings for this run
    orig_base_url = settings.openai_base_url
    orig_api_key = settings.openai_api_key
    orig_model = settings.ta_model
    orig_provider = settings.llm_provider

    settings.openai_base_url = base_url
    settings.openai_api_key = api_key
    settings.ta_model = model
    settings.llm_provider = provider

    # Force client re-initialization by clearing the module-level cache
    import scorer.llm
    scorer.llm._openai_client = None
    scorer.llm._anthropic_client = None

    results: list[tuple[Submission, ScoringResult, float]] = []
    total = len(submissions)

    console.print(f"\n[bold]{label}[/bold] ({model}) — scoring {total} submission(s)…")
    for i, sub in enumerate(submissions, 1):
        console.print(f"  [{i}/{total}] {sub.student_id} {sub.student_name} …", end=" ")
        start = time()
        try:
            result = score_submission(sub, rubric_text, prompt)
            elapsed = time() - start
            status = "[green]OK[/green]" if not result.needs_review else "[yellow]REVIEW[/yellow]"
            console.print(f"{status} {result.total_score:.0f}/{result.total_max:.0f} conf={result.confidence:.2f} ({elapsed:.1f}s)")
            results.append((sub, result, elapsed))
        except Exception as e:
            elapsed = time() - start
            console.print(f"[red]ERROR[/red] ({elapsed:.1f}s): {e}")
            # Placeholder result so comparison can still run
            results.append((
                sub,
                ScoringResult(
                    student_id=sub.student_id,
                    student_name=sub.student_name,
                    bb_user_id=sub.bb_user_id,
                    assignment_id=sub.assignment_id,
                    total_score=0.0,
                    total_max=100.0,
                    breakdown=[CriterionScore(criterion="ERROR", points_awarded=0, points_max=100, reasoning=str(e))],
                    uncertain_parts=[],
                    confidence=0.0,
                    llm_reasoning=f"ERROR: {e}",
                    needs_review=True,
                    processing_notes="error",
                ),
                elapsed,
            ))

    # Restore settings
    settings.openai_base_url = orig_base_url
    settings.openai_api_key = orig_api_key
    settings.ta_model = orig_model
    settings.llm_provider = orig_provider
    scorer.llm._openai_client = None
    scorer.llm._anthropic_client = None

    return results


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------

def _breakdown_to_dict(breakdown: list[CriterionScore]) -> dict[str, float]:
    return {b.criterion: b.points_awarded for b in breakdown}


def _diff_breakdown(a: list[CriterionScore], b: list[CriterionScore]) -> list[str]:
    """Return human-readable differences between two breakdowns."""
    ad = _breakdown_to_dict(a)
    bd = _breakdown_to_dict(b)
    diffs: list[str] = []
    all_keys = set(ad) | set(bd)
    for k in sorted(all_keys):
        av = ad.get(k, 0.0)
        bv = bd.get(k, 0.0)
        if abs(av - bv) > 0.5:
            diffs.append(f"{k}: {av:.0f} vs {bv:.0f}")
    return diffs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Compare two LLM models on the same homework submissions.")
    parser.add_argument("--assignment-dir", required=True, help="Directory with cached submission files")
    parser.add_argument("--rubric", required=True, type=Path, help="Path to rubric file")
    parser.add_argument("--prompt", default="prompts/system_zh.md", type=Path, help="System prompt file")
    parser.add_argument("--count", type=int, default=5, help="Number of submissions to test")
    args = parser.parse_args()

    assignment_dir = Path(args.assignment_dir)
    if not assignment_dir.exists():
        console.print(f"[red]Assignment directory not found: {assignment_dir}[/red]")
        return

    rubric_text = args.rubric.read_text(encoding="utf-8")
    submissions = load_cached_submissions(assignment_dir, args.count)
    if not submissions:
        console.print("[yellow]No submissions found.[/yellow]")
        return

    console.print(f"Loaded {len(submissions)} submission(s) from [cyan]{assignment_dir}[/cyan]")

    # --- Model A: kimi (from .env) ---
    kimi_results = score_with_model(
        submissions, rubric_text, args.prompt,
        base_url=settings.openai_base_url,
        api_key=settings.openai_api_key,
        model=settings.ta_model,
        label="Model A (kimi / .env)",
        provider=settings.llm_provider,
    )

    # --- Model B: qwen (dashscope) ---
    dashscope_key = _read_env_key("DASHSCOPE_API_KEY")
    if not dashscope_key:
        console.print("[red]Error:[/red] No qwen API key. Set DASHSCOPE_API_KEY in .env.")
        return
    qwen_results = score_with_model(
        submissions, rubric_text, args.prompt,
        base_url=QWEN_BASE_URL,
        api_key=dashscope_key,
        model=QWEN_MODEL,
        label="Model B (qwen / dashscope)",
    )

    # --- Build comparison table ---
    table = Table(title="Model Comparison")
    table.add_column("Student", style="cyan", no_wrap=True)
    table.add_column("Name", style="cyan")
    table.add_column("kimi Score", justify="right")
    table.add_column("qwen Score", justify="right")
    table.add_column("Δ", justify="right")
    table.add_column("kimi conf", justify="right", style="dim")
    table.add_column("qwen conf", justify="right", style="dim")
    table.add_column("kimi time", justify="right", style="dim")
    table.add_column("qwen time", justify="right", style="dim")
    table.add_column("Disagreements", style="yellow")

    total_kimi = 0.0
    total_qwen = 0.0
    total_diff = 0.0
    disagreements = 0

    for (sub, kimi_res, kimi_t), (_, qwen_res, qwen_t) in zip(kimi_results, qwen_results):
        diff = abs(kimi_res.total_score - qwen_res.total_score)
        total_kimi += kimi_res.total_score
        total_qwen += qwen_res.total_score
        total_diff += diff
        if diff > 0.5:
            disagreements += 1

        # Breakdown diffs
        bd_diffs = _diff_breakdown(kimi_res.breakdown, qwen_res.breakdown)
        flag_diffs: list[str] = []
        if kimi_res.needs_review != qwen_res.needs_review:
            flag_diffs.append("review flag")
        if len(kimi_res.uncertain_parts) != len(qwen_res.uncertain_parts):
            flag_diffs.append(f"uncertain {len(kimi_res.uncertain_parts)} vs {len(qwen_res.uncertain_parts)}")
        if bd_diffs:
            flag_diffs.extend(bd_diffs[:3])  # cap at 3
        disagree_text = "; ".join(flag_diffs) if flag_diffs else "—"

        diff_style = "red" if diff > 5 else ("yellow" if diff > 0 else "green")
        table.add_row(
            sub.student_id,
            sub.student_name,
            f"{kimi_res.total_score:.0f}/{kimi_res.total_max:.0f}",
            f"{qwen_res.total_score:.0f}/{qwen_res.total_max:.0f}",
            f"[{diff_style}]{diff:+.0f}[/{diff_style}]",
            f"{kimi_res.confidence:.2f}",
            f"{qwen_res.confidence:.2f}",
            f"{kimi_t:.1f}s",
            f"{qwen_t:.1f}s",
            disagree_text,
        )

    # Summary row
    avg_kimi = total_kimi / len(submissions) if submissions else 0
    avg_qwen = total_qwen / len(submissions) if submissions else 0
    avg_diff = total_diff / len(submissions) if submissions else 0
    table.add_row(
        "[bold]AVG[/bold]", "",
        f"[bold]{avg_kimi:.1f}[/bold]",
        f"[bold]{avg_qwen:.1f}[/bold]",
        f"[bold]{avg_diff:.1f}[/bold]",
        "", "", "", "",
        f"[bold]{disagreements}/{len(submissions)} disagreed[/bold]",
    )
    console.print(table)

    # Additional stats
    console.print(f"\n[bold]Summary[/bold]")
    console.print(f"  Total score difference: {total_diff:.0f} pts across {len(submissions)} students")
    console.print(f"  Average difference: {avg_diff:.1f} pts")
    console.print(f"  Students with >5pt diff: {sum(1 for (_, k, _), (_, q, _) in zip(kimi_results, qwen_results) if abs(k.total_score - q.total_score) > 5)}")

    # Save raw results to JSON for deeper analysis
    output_path = Path("benchmark_results.json")
    raw_data = []
    for (sub, kimi_res, kimi_t), (_, qwen_res, qwen_t) in zip(kimi_results, qwen_results):
        raw_data.append({
            "student_id": sub.student_id,
            "student_name": sub.student_name,
            "kimi": {
                "score": kimi_res.total_score,
                "max": kimi_res.total_max,
                "confidence": kimi_res.confidence,
                "needs_review": kimi_res.needs_review,
                "breakdown": [{"criterion": b.criterion, "awarded": b.points_awarded, "max": b.points_max, "reasoning": b.reasoning} for b in kimi_res.breakdown],
                "uncertain_parts": [{"description": u.description, "suggested": u.suggested_score, "suggested_max": u.suggested_max} for u in kimi_res.uncertain_parts],
                "llm_reasoning": kimi_res.llm_reasoning,
                "student_feedback": kimi_res.student_feedback,
                "time": kimi_t,
            },
            "qwen": {
                "score": qwen_res.total_score,
                "max": qwen_res.total_max,
                "confidence": qwen_res.confidence,
                "needs_review": qwen_res.needs_review,
                "breakdown": [{"criterion": b.criterion, "awarded": b.points_awarded, "max": b.points_max, "reasoning": b.reasoning} for b in qwen_res.breakdown],
                "uncertain_parts": [{"description": u.description, "suggested": u.suggested_score, "suggested_max": u.suggested_max} for u in qwen_res.uncertain_parts],
                "llm_reasoning": qwen_res.llm_reasoning,
                "student_feedback": qwen_res.student_feedback,
                "time": qwen_t,
            },
        })
    output_path.write_text(json.dumps(raw_data, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"\n[dim]Raw results saved to {output_path}[/dim]")


if __name__ == "__main__":
    main()
