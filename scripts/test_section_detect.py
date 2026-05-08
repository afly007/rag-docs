#!/usr/bin/env python3
"""Inspect section detection for a PDF — no OpenAI or Qdrant calls needed.

Usage:
    python scripts/test_section_detect.py docs/your-doc.pdf
    python scripts/test_section_detect.py docs/your-doc.pdf --top 40
"""

import argparse
import re
import statistics
import sys
from pathlib import Path

import fitz
import tiktoken

CHUNK_SIZE = 750
CHUNK_OVERLAP = 100
MIN_SECTION_TOKENS = 100

enc = tiktoken.get_encoding("cl100k_base")


def extract_toc_sections(doc: fitz.Document) -> list[dict] | None:
    toc = doc.get_toc(simple=False)
    if not toc:
        return None

    entries: list[tuple[int, str, int, float]] = []
    for level, title, page_1idx, dest in toc:
        start_page = dest.get("page", page_1idx - 1)
        to_point = dest.get("to")
        start_y = to_point.y if to_point is not None else 0.0
        clean_title = re.sub(r"\s+", " ", title.strip())
        entries.append((level, clean_title, start_page, start_y))

    n = len(entries)
    sections = []
    for i, (level, title, start_page, start_y) in enumerate(entries):
        end_page = len(doc) - 1
        end_y = float("inf")
        for j in range(i + 1, n):
            if entries[j][0] <= level:
                end_page = entries[j][2]
                end_y = entries[j][3]
                break
        has_children = (i + 1 < n) and (entries[i + 1][0] > level)
        sections.append(
            {
                "level": level,
                "title": title,
                "start_page": start_page,
                "start_y": start_y,
                "end_page": end_page,
                "end_y": end_y,
                "has_children": has_children,
            }
        )
    return sections


def _build_page_blocks_cache(doc: fitz.Document) -> list[list[tuple[float, str]]]:
    cache = []
    for page in doc:
        blocks = []
        for block in page.get_text("blocks"):
            if block[6] == 0:
                blocks.append((block[1], block[4]))
        cache.append(blocks)
    return cache


def _section_tokens(sec: dict, blocks_cache: list) -> int:
    num_pages = len(blocks_cache)
    count = 0
    for pg_idx in range(sec["start_page"], min(sec["end_page"] + 1, num_pages)):
        for y0, text in blocks_cache[pg_idx]:
            if pg_idx == sec["start_page"] and sec["start_y"] > 5 and y0 < sec["start_y"] - 5:
                continue
            if pg_idx == sec["end_page"] and sec["end_y"] != float("inf") and y0 >= sec["end_y"]:
                continue
            count += len(enc.encode(text))
    return count


def _chunks_for_tokens(n: int) -> int:
    if n == 0:
        return 0
    if n <= CHUNK_SIZE:
        return 1
    return (n - CHUNK_OVERLAP + CHUNK_SIZE - CHUNK_OVERLAP - 1) // (CHUNK_SIZE - CHUNK_OVERLAP)


def main():
    parser = argparse.ArgumentParser(description="Test PDF section detection")
    parser.add_argument("pdf", help="Path to a PDF file")
    parser.add_argument("--top", type=int, default=20, help="Leaf sections to preview (default 20)")
    args = parser.parse_args()

    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    doc = fitz.open(pdf_path)
    toc = doc.get_toc(simple=False)
    print(f"File  : {pdf_path.name}  ({pdf_path.stat().st_size / 1_048_576:.1f} MB)")
    print(f"Pages : {len(doc)}")
    print(f"TOC   : {len(toc)} entries")

    if not toc:
        print("\nNo TOC found — fixed-stride fallback would be used.")
        sys.exit(0)

    sections = extract_toc_sections(doc)
    leaf_sections = [s for s in sections if not s["has_children"]]

    level_counts: dict[int, int] = {}
    for s in sections:
        level_counts[s["level"]] = level_counts.get(s["level"], 0) + 1
    print("\nTOC level distribution:")
    for lvl in sorted(level_counts):
        print(f"  L{lvl}: {level_counts[lvl]:,} sections")
    print(f"  Leaf: {len(leaf_sections):,} sections")

    print("\nBuilding page blocks cache…", end="", flush=True)
    blocks_cache = _build_page_blocks_cache(doc)
    print(" done")

    token_counts = [_section_tokens(s, blocks_cache) for s in leaf_sections]
    if not token_counts:
        print("No leaf sections found.")
        sys.exit(0)

    print("\nLeaf section token stats:")
    print(f"  min    : {min(token_counts):,}")
    print(f"  median : {statistics.median(token_counts):,.0f}")
    print(f"  mean   : {statistics.mean(token_counts):,.0f}")
    print(f"  max    : {max(token_counts):,}")

    buckets = [
        ("0 tokens (empty)", lambda t: t == 0),
        ("1–99 tokens (merge)", lambda t: 1 <= t < MIN_SECTION_TOKENS),
        ("100–750 tokens (1 chunk)", lambda t: MIN_SECTION_TOKENS <= t <= CHUNK_SIZE),
        ("751–2 000 tokens (2–3 chunks)", lambda t: CHUNK_SIZE < t <= 2000),
        (">2 000 tokens (large)", lambda t: t > 2000),
    ]
    print("\nToken histogram:")
    for label, fn in buckets:
        n = sum(1 for t in token_counts if fn(t))
        pct = n / len(token_counts) * 100
        print(f"  {label:<35} {n:>5} ({pct:>4.0f}%)")

    expected_sections = sum(1 for t in token_counts if t >= MIN_SECTION_TOKENS)
    expected_chunks = sum(_chunks_for_tokens(t) for t in token_counts if t >= MIN_SECTION_TOKENS)
    total_tokens = sum(len(enc.encode(p.get_text())) for p in doc)
    fixed_chunks = _chunks_for_tokens(total_tokens)

    print("\nExpected output:")
    print(f"  Sections after merge  : {expected_sections:,}")
    print(f"  Chunks (section-aware): {expected_chunks:,}")
    print(f"  Chunks (fixed-stride) : {fixed_chunks:,}")
    print(
        f"  Sections needing split (>{CHUNK_SIZE} tokens): "
        f"{sum(1 for t in token_counts if t > CHUNK_SIZE)}"
    )

    anomalous = [(leaf_sections[i]["title"], t) for i, t in enumerate(token_counts) if t > 10_000]
    if anomalous:
        print("\nAnomalously large sections (>10k tokens):")
        for title, t in anomalous[:10]:
            print(f"  {t:>8,} tokens  {title[:60]}")

    preview_n = min(args.top, len(leaf_sections))
    print(f"\nFirst {preview_n} leaf sections:")
    print(f"  {'L':<3} {'tokens':>7} {'chunks':>7}  title")
    print(f"  {'─' * 3} {'─' * 7} {'─' * 7}  {'─' * 40}")
    for sec, tok in zip(leaf_sections[:preview_n], token_counts[:preview_n]):
        nchunks = _chunks_for_tokens(tok)
        flag = " (merge)" if 0 < tok < MIN_SECTION_TOKENS else (" (empty)" if tok == 0 else "")
        print(f"  L{sec['level']:<2} {tok:>7,} {nchunks:>7}  {sec['title'][:50]}{flag}")


if __name__ == "__main__":
    main()
