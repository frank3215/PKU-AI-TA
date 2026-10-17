"""
Split PDFs by top-level chapter bookmarks.

Usage:
    uv run python split_pdf_by_chapters.py

Creates output directories:
    - 教材/          for 屈婉玲-算法设计与分析-第2版.pdf
    - 习题解答/       for 算法设计与分析习题解答与学习指导（第2版）.pdf
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import fitz  # pymupdf


def sanitize_filename(name: str) -> str:
    """Remove/replace characters unsafe for filenames."""
    name = name.strip().replace("\u3000", " ")  # full-width space
    # replace filesystem-unsafe chars
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    # collapse multiple spaces/underscores
    name = re.sub(r"[\s_]+", "_", name)
    return name.strip("_")


def get_chapter_ranges(toc: list[tuple[int, str, int]]) -> list[tuple[str, int, int]]:
    """
    From a flat TOC list, extract top-level (level==0) entries that look like chapters.
    Returns list of (title, start_page_0based, end_page_0based_inclusive).
    """
    # Filter top-level entries (pymupdf levels are 1-based)
    level0 = [(lvl, title, page) for lvl, title, page in toc if lvl == 1]

    # Keep only entries that look like chapters or useful appendix material
    # We'll include anything from "第1章" onward, plus "参考文献"
    chapter_pattern = re.compile(r"第\d+章")
    keep = []
    started = False
    for lvl, title, page in level0:
        if chapter_pattern.search(title):
            started = True
        if not started:
            continue
        # Skip the very last "封底" page
        if "封底" in title:
            continue
        keep.append((title, page))

    # Compute ranges: start_page to next_start_page - 1
    ranges: list[tuple[str, int, int]] = []
    for i, (title, page) in enumerate(keep):
        start = page - 1  # convert 1-based bookmark page to 0-based fitz index
        if i + 1 < len(keep):
            end = keep[i + 1][1] - 2  # page before next bookmark start
        else:
            end = None  # will use last page
        ranges.append((title, start, end))

    return ranges


def split_pdf(src_path: Path, out_dir: Path, prefix: str = "") -> None:
    doc = fitz.open(src_path)
    toc = doc.get_toc()
    total_pages = len(doc)

    if not toc:
        print(f"No bookmarks in {src_path.name}, skipping.")
        return

    ranges = get_chapter_ranges(toc)
    if not ranges:
        print(f"No chapter bookmarks found in {src_path.name}, skipping.")
        return

    out_dir.mkdir(parents=True, exist_ok=True)

    for idx, (title, start, end) in enumerate(ranges, 1):
        end = end if end is not None else total_pages - 1
        if start > end or start >= total_pages:
            print(f"  Skipping {title}: invalid page range [{start}-{end}]")
            continue

        page_indices = list(range(start, end + 1))
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=start, to_page=end)

        # Build filename: prefix-Ch01-基础知识.pdf
        chapter_num_match = re.search(r"第(\d+)章", title)
        if chapter_num_match:
            ch_num = int(chapter_num_match.group(1))
            ch_label = f"Ch{ch_num:02d}"
        else:
            ch_label = f"Sec{idx:02d}"

        short_title = re.sub(r"^第\d+章\s*[\s　]*", "", title)
        safe_title = sanitize_filename(short_title) if short_title else sanitize_filename(title)

        filename = f"{prefix}-{ch_label}-{safe_title}.pdf" if prefix else f"{ch_label}-{safe_title}.pdf"
        out_path = out_dir / filename

        new_doc.save(out_path, garbage=4, deflate=True)
        new_doc.close()
        print(f"  {out_path.name}  ({len(page_indices)} pages, p{start + 1}-p{end + 1})")

    doc.close()
    print(f"Done. Output → {out_dir}/\n")


def main() -> None:
    jobs = [
        (
            Path("屈婉玲-算法设计与分析-第2版.pdf"),
            Path("教材"),
            "教材",
        ),
        (
            Path("算法设计与分析习题解答与学习指导（第2版） (屈婉玲, 刘田, 张立昂, 王捍贫) (Z-Libra_202603181402_59707.pdf"),
            Path("习题解答"),
            "习题解答",
        ),
    ]

    for src, out_dir, prefix in jobs:
        if not src.exists():
            print(f"Not found: {src}")
            continue
        print(f"Splitting: {src.name}")
        split_pdf(src, out_dir, prefix)


if __name__ == "__main__":
    main()
