"""
LLM-based scorer using OpenAI-compatible API (OpenRouter → Qwen).

Scoring pipeline per submission:
  1. Try to extract text from the attachment (pypdf for PDFs, python-docx for Word).
  2. If text extraction yields nothing (scanned/image PDF or image file), convert PDF
     pages to images via pymupdf and send them to the vision model instead.
  3. Ask the model to return a structured JSON scoring result.
  4. Parse the JSON into a ScoringResult.
"""
from __future__ import annotations

import base64
import io
import json
import re

from openai import OpenAI

from config import settings
from models import Attachment, CriterionScore, ScoringResult, Submission, UncertainPart

_client: OpenAI | None = None

_UNREADABLE_MARKERS = (
    "no extractable text",
    "image file",
    "image-only",
    "image format",
    "manual review required",
    "Could not extract text",
    "Unknown file format",
)


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
        )
    return _client


SYSTEM_PROMPT_EN = """\
You are a kind and supportive teaching assistant grading student homework for an \
Algorithm Design and Analysis course.

Grading philosophy:
- Default to FULL marks. Only deduct when you have clear, direct evidence of an error \
or a genuinely missing required component visible in the submission.
- When in doubt — if you are unsure whether something is correct, incomplete, or \
missing — do NOT deduct. Award full marks for that part and record it in uncertain_parts \
for human review instead.
- Every deduction MUST be accompanied by an explicit reason and the exact points deducted \
(e.g. "deduct 2 pts: space complexity analysis is absent"). No reason = no deduction.
- Never deduct speculatively, for things you cannot clearly see, or because a step \
"might" be wrong.

Return ONLY a JSON object (no markdown fences, no extra text):
{
  "total_score": <float>,
  "total_max": <float>,
  "confidence": <float 0–1>,
  "breakdown": [
    {
      "criterion": "<problem number and name, e.g. 1.2 Selection Sort>",
      "points_awarded": <float>,
      "points_max": <float>,
      "reasoning": "<describe what is correct; for any deduction write: 'deduct X pts: <specific reason>'. If no deduction, state why full marks are awarded.>"
    }
  ],
  "uncertain_parts": [
    {
      "description": "<part you are unsure about — full marks already awarded, flagged for human to verify>",
      "suggested_score": <float>,
      "suggested_max": <float>
    }
  ],
  "llm_reasoning": "<brief overall summary: strengths, confirmed deductions with reasons, uncertain parts>"
}

Rules:
- List EVERY rubric criterion in breakdown, even if full marks.
- If a part is uncertain, award full marks AND list it in uncertain_parts (do not deduct).
- confidence < 0.75 → you are unsure about the overall score; list ambiguous parts in uncertain_parts.
- Do NOT invent criteria not in the rubric.
- All numeric fields must be numbers, never null.
"""

SYSTEM_PROMPT_ZH = """\
你是一位友善且乐于助人的助教，正在为《算法设计与分析》课程批改学生作业。

评分原则：
- 默认给满分。只有当你有明确、直接的证据证明作业中有错误，或者确实缺少要求的内容时，才扣分。
- 如有疑问 —— 如果你不确定某部分是否正确、完整或缺失 —— 不要扣分。给这部分打满分，并将其记录在 uncertain_parts 中供人工复核。
- 每一项扣分都必须有明确的理由和具体的扣分数（例如："扣2分：缺少空间复杂度分析"）。没有理由 = 不扣分。
- 永远不要因为看不清、或某一步"可能"错误而猜测性地扣分。

只返回一个JSON对象（不要markdown格式，不要额外文字）：
{
  "total_score": <float>,
  "total_max": <float>,
  "confidence": <float 0–1>,
  "breakdown": [
    {
      "criterion": "<题目编号和名称，例如：1.2 选择排序>",
      "points_awarded": <float>,
      "points_max": <float>,
      "reasoning": "<描述正确的部分；如有扣分，请写：'扣X分：<具体原因>'。如果没有扣分，说明为什么给满分。>"
    }
  ],
  "uncertain_parts": [
    {
      "description": "<你不确定的部分 —— 已给满分，标记供人工核实>",
      "suggested_score": <float>,
      "suggested_max": <float>
    }
  ],
  "llm_reasoning": "<简要的整体总结：优点、确认的扣分及理由、不确定的部分>"
}

规则：
- 在 breakdown 中列出评分标准中的每一项，即使是满分也要列出。
- 如果某部分不确定，给满分并在 uncertain_parts 中列出（不要扣分）。
- confidence < 0.75 → 你对整体评分不确定；在 uncertain_parts 中列出模糊不清的部分。
- 不要编造评分标准中没有的评判项。
- 所有数值字段必须是数字，不能为null。
"""

SYSTEM_PROMPT = SYSTEM_PROMPT_EN


def get_system_prompt(language: str = "en") -> str:
    """Get the system prompt in the specified language ("en" or "zh")."""
    if language.lower() in ("zh", "cn", "chinese", "中文"):
        return SYSTEM_PROMPT_ZH
    return SYSTEM_PROMPT_EN


# ---------------------------------------------------------------------------
# Attachment handling
# ---------------------------------------------------------------------------

def _extract_text(attachment: Attachment) -> str:
    """Extract text from a PDF, Word doc, or plain-text file."""
    data = attachment.data
    fname = attachment.filename.lower()

    if data[:4] == b'%PDF' or fname.endswith('.pdf'):
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            pages = [page.extract_text() or "" for page in reader.pages]
            text = "\n\n".join(pages).strip()
            return text if text else "(PDF has no extractable text — may be scanned/image-only)"
        except Exception as e:
            return f"(Could not extract text from PDF: {e})"

    if data[:2] == b'PK' or fname.endswith(('.docx', '.doc')):
        try:
            from docx import Document
            doc = Document(io.BytesIO(data))
            text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            return text if text else "(Word document has no extractable text)"
        except Exception as e:
            return f"(Could not extract text from Word document: {e})"

    if data[:3] == b'\xff\xd8\xff' or fname.endswith(('.jpg', '.jpeg', '.png')):
        return "(Submission is an image file — text extraction not supported)"

    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return f"(Unknown file format for {attachment.filename})"


def _needs_vision(text: str) -> bool:
    if any(marker in text for marker in _UNREADABLE_MARKERS):
        return True
    # Very short extracted text almost certainly means watermarks only, not real content
    return len(text.strip()) < 200


def _pdf_to_image_parts(data: bytes) -> list[dict]:
    """Render PDF pages to images for vision model.

    Always renders pages directly rather than trying to extract embedded images,
    because PDFs often contain non-content images (ICC profiles, thumbnails, etc.)
    that aren't student work. Rendering ensures we see exactly what a human sees.
    """
    import fitz  # pymupdf
    doc = fitz.open(stream=data, filetype="pdf")
    parts = []
    for page in doc:
        mat = fitz.Matrix(2.0, 2.0)
        pix = page.get_pixmap(matrix=mat)
        b64 = base64.b64encode(pix.tobytes("png")).decode()
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"},
        })
    return parts


def _attachment_content_parts(attachment: Attachment) -> list[dict]:
    """
    Return content parts for one attachment.

    Tries text extraction first. For scanned/image files falls back to sending
    the raw image bytes as image_url so the model can read them visually.
    """
    text = _extract_text(attachment)
    if not _needs_vision(text):
        return [{"type": "text", "text": f"**File: {attachment.filename}**\n\n{text}"}]

    data = attachment.data
    fname = attachment.filename.lower()

    # JPEG submitted directly
    if data[:3] == b'\xff\xd8\xff' or fname.endswith(('.jpg', '.jpeg')):
        b64 = base64.b64encode(data).decode()
        return [
            {"type": "text", "text": f"**File: {attachment.filename}** (image)"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}},
        ]

    # PNG submitted directly
    if data[:8] == b'\x89PNG\r\n\x1a\n' or fname.endswith('.png'):
        b64 = base64.b64encode(data).decode()
        return [
            {"type": "text", "text": f"**File: {attachment.filename}** (image)"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}},
        ]

    # Scanned PDF — render pages to images
    if data[:4] == b'%PDF' or fname.endswith('.pdf'):
        try:
            img_parts = _pdf_to_image_parts(data)
            if img_parts:
                return (
                    [{"type": "text", "text": f"**File: {attachment.filename}** (scanned — pages rendered)"}]
                    + img_parts
                )
        except Exception:
            pass

    # Unrenderable — pass the error text so the model knows
    return [{"type": "text", "text": f"**File: {attachment.filename}**\n\n{text}"}]


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_submission(submission: Submission, rubric: str, language: str = "en") -> ScoringResult:
    """Score a single submission against the rubric using the LLM."""
    content: list[dict] = [
        {"type": "text", "text": f"## Scoring Rubric\n\n{rubric}\n\n## Student Submission"},
    ]

    if submission.text_content.strip():
        content.append({"type": "text", "text": f"**Text answer:**\n{submission.text_content}"})

    for att in submission.attachments:
        content.extend(_attachment_content_parts(att))

    extra: dict = {}
    if not settings.enable_thinking:
        # Only send enable_thinking for Qwen models on platforms that support it
        # Skip for Volcengine/other providers that don't support this parameter
        model_name = settings.ta_model.lower()
        if "qwen" in model_name and ("openrouter" in settings.openai_base_url or "dashscope" in settings.openai_base_url):
            extra["extra_body"] = {"enable_thinking": False}

    response = _get_client().chat.completions.create(
        model=settings.ta_model,
        messages=[
            {"role": "system", "content": get_system_prompt(language)},
            {"role": "user", "content": content},
        ],
        temperature=0.2,
        **extra,
    )

    raw = response.choices[0].message.content or ""
    data = _parse_json(raw)

    def _f(val, default: float = 0.0) -> float:
        return float(val) if val is not None else default

    uncertain = [
        UncertainPart(
            description=u.get("description") or u.get("criterion") or u.get("part") or str(u),
            suggested_score=_f(u.get("suggested_score")),
            suggested_max=_f(u.get("suggested_max")),
        )
        for u in data.get("uncertain_parts", [])
    ]
    breakdown = [
        CriterionScore(
            criterion=b.get("criterion", ""),
            points_awarded=_f(b.get("points_awarded")),
            points_max=_f(b.get("points_max")),
            reasoning=b.get("reasoning", ""),
        )
        for b in data.get("breakdown", [])
    ]
    confidence = _f(data.get("confidence"), 1.0)

    return ScoringResult(
        student_id=submission.student_id,
        student_name=submission.student_name,
        bb_user_id=submission.bb_user_id,
        assignment_id=submission.assignment_id,
        total_score=_f(data.get("total_score")),
        total_max=_f(data.get("total_max")),
        breakdown=breakdown,
        uncertain_parts=uncertain,
        confidence=confidence,
        llm_reasoning=data.get("llm_reasoning", ""),
        needs_review=confidence < settings.review_threshold or len(uncertain) > 0,
    )


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

def _sanitize_json(s: str) -> str:
    """Escape stray backslashes (e.g. LaTeX \\frac) that make JSON invalid."""
    return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)


def _parse_json(raw: str) -> dict:
    """Extract JSON from model output, tolerating minor formatting issues."""
    cleaned = re.sub(r"```(?:json)?\s*|```", "", raw).strip()
    for candidate in [cleaned, _sanitize_json(cleaned)]:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        block = match.group()
        for candidate in [block, _sanitize_json(block)]:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
    raise ValueError(f"Could not parse LLM response as JSON:\n{raw[:500]}")
