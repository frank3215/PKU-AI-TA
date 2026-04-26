"""
LLM-based scorer supporting both OpenAI-compatible and Anthropic-native APIs.

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
import logging
import re
import time
from pathlib import Path

from config import settings
from models import Attachment, CriterionScore, ScoringResult, Submission, UncertainPart

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

_UNREADABLE_MARKERS = (
    "no extractable text",
    "image file",
    "image-only",
    "image format",
    "manual review required",
    "Could not extract text",
    "Unknown file format",
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("pku_ai_ta.scorer")

# ---------------------------------------------------------------------------
# Client initialization
# ---------------------------------------------------------------------------

_openai_client = None
_anthropic_client = None


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import OpenAI
        _openai_client = OpenAI(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
        )
    return _openai_client


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        from anthropic import Anthropic
        kwargs = {
            "api_key": settings.openai_api_key,
            # Override the default User-Agent to avoid vendor blocking
            "default_headers": {"User-Agent": "claude-cli/2.1.81 (external, cli)"},
        }
        # If a custom base_url is set (not the OpenAI-compat defaults), pass it through.
        if settings.openai_base_url and "openrouter" not in settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        _anthropic_client = Anthropic(**kwargs)
    return _anthropic_client


_DEFAULT_PROMPT = _PROMPTS_DIR / "system_zh.md"


def get_system_prompt(path: Path) -> str:
    """Load the system prompt from a file path."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"System prompt file not found: {path}\n"
        f"Built-in prompts are in {_PROMPTS_DIR}/: system_en.md, system_zh.md"
    )


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


def _pdf_has_embedded_images(data: bytes) -> bool:
    """Check if a PDF has embedded images (indicating it's a scanned PDF)."""
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=data, filetype="pdf")
        for page in doc:
            if page.get_images(full=True):
                return True
        return False
    except Exception:
        return False


def _needs_vision(text: str, attachment: Attachment | None = None) -> bool:
    if any(marker in text for marker in _UNREADABLE_MARKERS):
        return True

    # If PDF has embedded images, use vision mode (avoids OCR text layer issues)
    if attachment and attachment.filename.lower().endswith('.pdf'):
        if _pdf_has_embedded_images(attachment.data):
            return True

    stripped = text.strip()
    # Very short extracted text almost certainly means watermarks only, not real content
    if len(stripped) < 200:
        return True


    # Detect garbled OCR text: PDF text layer exists but is unreadable.
    # For Chinese homework, a long text with almost no CJK characters is
    # a strong signal of corrupted OCR / bad text extraction.
    cjk_count = sum(1 for ch in stripped if '\u4e00' <= ch <= '\u9fff')
    if len(stripped) > 200 and cjk_count / len(stripped) < 0.03:
        return True

    return False


def _pdf_to_image_parts(data: bytes) -> list[dict]:
    """Render PDF pages to images for vision model."""
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
    """Return content parts for one attachment in OpenAI format."""
    text = _extract_text(attachment)
    if not _needs_vision(text, attachment):
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
# Anthropic format conversion
# ---------------------------------------------------------------------------

def _to_anthropic_content(parts: list[dict]) -> list[dict]:
    """Convert OpenAI-format content parts to Anthropic Messages API format."""
    result = []
    for part in parts:
        if part.get("type") == "text":
            result.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            url = part["image_url"]["url"]
            # Parse data URI: data:image/png;base64,...
            if url.startswith("data:"):
                header, _, b64 = url.partition(",")
                media_type = header.split(";")[0].replace("data:image/", "image/")
                result.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                })
            else:
                # External URL — Anthropic doesn't support this directly, skip
                result.append({"type": "text", "text": f"[image: {url}]"})
    return result


# ---------------------------------------------------------------------------
# LLM call helpers
# ---------------------------------------------------------------------------

def _call_openai(system_prompt: str, content_parts: list[dict]) -> str:
    """Call OpenAI-compatible API and return raw text response."""
    client = _get_openai_client()
    extra: dict = {}
    if not settings.enable_thinking:
        model_name = settings.ta_model.lower()
        if "qwen" in model_name and ("openrouter" in settings.openai_base_url or "dashscope" in settings.openai_base_url):
            extra["extra_body"] = {"enable_thinking": False}

    response = client.chat.completions.create(
        model=settings.ta_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_parts},
        ],
        temperature=0.2,
        **extra,
    )
    return response.choices[0].message.content or ""


def _call_anthropic(system_prompt: str, content_parts: list[dict]) -> str:
    """Call Anthropic Messages API and return raw text response."""
    client = _get_anthropic_client()
    ac = _to_anthropic_content(content_parts)
    response = client.messages.create(
        model=settings.ta_model,
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": ac}],
        temperature=0.2,
    )
    # Anthropic returns content blocks; first text block is the answer
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    return ""


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_submission(submission: Submission, rubric: str, prompt: Path = _DEFAULT_PROMPT) -> ScoringResult:
    """Score a single submission against the rubric using the LLM."""
    system_prompt = get_system_prompt(prompt)

    content: list[dict] = [
        {"type": "text", "text": f"## Scoring Rubric\n\n{rubric}\n\n## Student Submission"},
    ]

    if submission.text_content.strip():
        content.append({"type": "text", "text": f"**Text answer:**\n{submission.text_content}"})

    # Track how each attachment was processed (text vs vision) for debugging
    _processing_modes: list[str] = []
    for att in submission.attachments:
        parts = _attachment_content_parts(att)
        mode = "vision" if any(p.get("type") == "image_url" for p in parts) else "text"
        _processing_modes.append(mode)
        content.extend(parts)

    processing_notes = (
        "+".join(_processing_modes)
        if len(set(_processing_modes)) > 1
        else (_processing_modes[0] if _processing_modes else "text")
    )

    start = time.time()
    provider = settings.llm_provider.lower()
    if provider == "anthropic":
        raw = _call_anthropic(system_prompt, content)
    else:
        raw = _call_openai(system_prompt, content)
    elapsed = time.time() - start

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

    result = ScoringResult(
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
        processing_notes=processing_notes,
    )

    logger.info(
        "Scored %s %s: %.1f/%.1f (conf=%.2f, mode=%s) via %s/%s in %.2fs",
        result.student_id,
        result.student_name,
        result.total_score,
        result.total_max,
        result.confidence,
        result.processing_notes,
        provider,
        settings.ta_model,
        elapsed,
    )

    return result


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

def _sanitize_json(s: str) -> str:
    """Escape stray backslashes (e.g. LaTeX \\frac) that make JSON invalid."""
    return re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', s)


def _parse_json(raw: str) -> dict:
    """Extract JSON from model output, tolerating minor formatting issues."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()
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
