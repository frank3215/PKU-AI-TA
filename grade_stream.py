#!/usr/bin/env python3
"""Stream LLM scoring output in real-time.  Useful for watching the model think."""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import settings
from scorer.llm import get_system_prompt, _attachment_content_parts, _get_openai_client
from models import Submission, Attachment

# Config: adjust these
RUBRIC = Path("rubric5.md")
PROMPT = Path("prompts/system_zh.md")
STUDENT_SID = "2400017429"  # 侯诗怡
SUBMISSION_DIR = Path("submissions/426252")

# Find file
files = list(SUBMISSION_DIR.glob(f"{STUDENT_SID}_*")) or list(SUBMISSION_DIR.glob(f"*{STUDENT_SID}*"))
if not files:
    print(f"No submission found for {STUDENT_SID}")
    sys.exit(1)
fpath = files[0]
# Extract student name from filename (e.g. "2400017429_侯诗怡.pdf" → "侯诗怡")
stem = fpath.stem
name_part = stem[len(STUDENT_SID):].lstrip("_")
print(f"\033[1;33mStreaming grading for {fpath.name} ({name_part})\033[0m\n")

rubric_text = RUBRIC.read_text(encoding="utf-8")
system_prompt = get_system_prompt(PROMPT)

att = Attachment(filename=fpath.name, content_type="application/pdf", data=fpath.read_bytes())
sub = Submission(student_id=STUDENT_SID, student_name=name_part, assignment_id="426252", assignment_title="第五次作业", attachments=[att])

content = [
    {"type": "text", "text": f"## Scoring Rubric\n\n{rubric_text}\n\n## Student Submission"},
]
if sub.text_content.strip():
    content.append({"type": "text", "text": f"**Text answer:**\n{sub.text_content}"})
for att in sub.attachments:
    content.extend(_attachment_content_parts(att))

# If vision mode was used, inject anti-loop guard
if any(p.get("type") == "image_url" for p in content):
    content[0]["text"] = (
        "CRITICAL: Do NOT output think or thinking tags. Output raw JSON only.\n"
        "1. If handwriting is unclear -> mark uncertain_parts, do NOT re-read. Human will check.\n"
        "2. If torn between deducting 2 vs 3 points -> choose 2 (lenient), mark uncertain_parts, move on.\n"
        "3. Equivalent expression = full marks. Do not debate whether student notation is standard.\n"
        "4. Each problem: judge ONCE. First judgment = final judgment.\n"
        "5. Output raw JSON directly.\n\n"
        + content[0]["text"]
    )

client = _get_openai_client()

extra = {}
model_name = settings.ta_model.lower()
if not settings.enable_thinking:
    if "qwen" in model_name and ("openrouter" in settings.openai_base_url or "dashscope" in settings.openai_base_url):
        extra["extra_body"] = {"enable_thinking": False}
    elif "minimax" in model_name:
        extra["extra_body"] = {"enable_thinking": False}

print(f"Model: {settings.ta_model}  |  Base: {settings.openai_base_url}")
print(f"Prompt: {PROMPT}  |  Rubric: {RUBRIC}")
print(f"{'='*60}\n")

t0 = time.time()
stream = client.chat.completions.create(
    model=settings.ta_model,
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
    ],
    temperature=0,
    max_tokens=65536,
    stream=True,
    **extra,
)

full = ""
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        text = chunk.choices[0].delta.content
        full += text
        print(text, end="", flush=True)

elapsed = time.time() - t0
print(f"\n{'='*60}")
print(f"Done in {elapsed:.1f}s, {len(full)} chars")

# Try parsing
from scorer.llm import _parse_json
try:
    result = _parse_json(full)
    print(f"\n\033[1;32mParsed OK: score={result['total_score']}/{result['total_max']} conf={result['confidence']}\033[0m")
except Exception as e:
    print(f"\n\033[1;31mParse failed: {e}\033[0m")
