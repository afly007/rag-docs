"""Shared chunking and metadata logic used by both the ingest CLI and the MCP server."""

import json
import re
import uuid
from pathlib import Path

import fitz  # PyMuPDF
import tiktoken
from qdrant_client.models import SparseVector

# ── Constants ─────────────────────────────────────────────────────────────────

CHUNK_SIZE = 750
CHUNK_OVERLAP = 100
MIN_SECTION_TOKENS = 100
EMBED_BATCH = 100
UPSERT_BATCH = 200

KNOWN_META_KEYS = {
    "vendor",
    "product",
    "version",
    "doc_type",
    "trust_tier",
    "source_type",
    "author",
    "last_updated",
    "url",
}
INT_META_KEYS = {"trust_tier"}

PAYLOAD_KEYS = {
    "text",
    "source",
    "page",
    "chunk_index",
    "section_title",
    "section_level",
    "section_index",
    "trust_tier",
    "source_type",
    "author",
    "last_updated",
    "url",
}

HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)

enc = tiktoken.get_encoding("cl100k_base")


# ── Sparse vector ─────────────────────────────────────────────────────────────


def compute_sparse(text: str) -> SparseVector:
    counts: dict[int, float] = {}
    for tid in enc.encode(text):
        counts[tid] = counts.get(tid, 0.0) + 1.0
    return SparseVector(indices=list(counts), values=list(counts.values()))


# ── Sidecar metadata ──────────────────────────────────────────────────────────


def load_sidecar(path: Path) -> dict:
    """Load optional <filename>.json sidecar with metadata for this file."""
    sidecar = path.with_suffix(".json")
    if not sidecar.exists():
        return {}
    try:
        with open(sidecar) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        result: dict = {}
        for k, v in data.items():
            if v is None:
                continue
            if k in INT_META_KEYS:
                try:
                    result[k] = int(v)
                except (ValueError, TypeError):
                    pass
            else:
                result[k] = str(v)
        return result
    except Exception:
        return {}


# ── PDF section-aware chunking ────────────────────────────────────────────────


def extract_toc_sections(doc: fitz.Document) -> list[dict] | None:
    """Extract section boundaries from the PDF's bookmark tree.

    Returns None when the document has no TOC (triggers fixed-stride fallback).
    """
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
    """Pre-cache (y0, text) tuples per page for fast section text extraction."""
    cache = []
    for page in doc:
        blocks = []
        for block in page.get_text("blocks"):
            if block[6] == 0:
                blocks.append((block[1], block[4]))
        cache.append(blocks)
    return cache


def chunk_document_sections(doc: fitz.Document, source: str, meta: dict) -> list[dict] | None:
    """Section-aware chunking using the PDF's TOC as section boundaries.

    Returns None when no TOC is found — caller falls back to chunk_document().
    """
    sections = extract_toc_sections(doc)
    if sections is None:
        return None

    blocks_cache = _build_page_blocks_cache(doc)
    num_pages = len(blocks_cache)

    section_data: list[dict] = []
    for sec in sections:
        if sec["has_children"]:
            continue

        tokens: list[int] = []
        token_pages: list[int] = []

        for pg_idx in range(sec["start_page"], min(sec["end_page"] + 1, num_pages)):
            for y0, text in blocks_cache[pg_idx]:
                if pg_idx == sec["start_page"] and sec["start_y"] > 5 and y0 < sec["start_y"] - 5:
                    continue
                if (
                    pg_idx == sec["end_page"]
                    and sec["end_y"] != float("inf")
                    and y0 >= sec["end_y"]
                ):
                    continue
                block_tokens = enc.encode(text)
                tokens.extend(block_tokens)
                token_pages.extend([pg_idx + 1] * len(block_tokens))

        if tokens:
            section_data.append(
                {
                    "title": sec["title"],
                    "level": sec["level"],
                    "tokens": tokens,
                    "token_pages": token_pages,
                }
            )

    if not section_data:
        return None

    merged: list[dict] = []
    buf_tokens: list[int] = []
    buf_pages: list[int] = []
    buf_title: str = ""
    buf_level: int = 1

    for sec in section_data:
        if buf_tokens:
            buf_tokens.extend(sec["tokens"])
            buf_pages.extend(sec["token_pages"])
        else:
            buf_tokens = sec["tokens"][:]
            buf_pages = sec["token_pages"][:]
            buf_title = sec["title"]
            buf_level = sec["level"]

        if len(buf_tokens) >= MIN_SECTION_TOKENS:
            merged.append(
                {
                    "title": buf_title,
                    "level": buf_level,
                    "tokens": buf_tokens,
                    "token_pages": buf_pages,
                }
            )
            buf_tokens = []
            buf_pages = []

    if buf_tokens:
        if merged:
            merged[-1]["tokens"].extend(buf_tokens)
            merged[-1]["token_pages"].extend(buf_pages)
        else:
            merged.append(
                {
                    "title": buf_title,
                    "level": buf_level,
                    "tokens": buf_tokens,
                    "token_pages": buf_pages,
                }
            )

    chunks: list[dict] = []
    chunk_index = 0

    for sec_idx, sec in enumerate(merged):
        tokens = sec["tokens"]
        token_pages = sec["token_pages"]
        title = sec["title"]
        level = sec["level"]

        sub_chunk = 0
        start = 0
        while start < len(tokens):
            end = min(start + CHUNK_SIZE, len(tokens))
            chunk_text = enc.decode(tokens[start:end])
            if sub_chunk > 0:
                chunk_text = f"{title}\n{chunk_text}"
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{title}:{sub_chunk}"))
            chunks.append(
                {
                    "id": chunk_id,
                    "text": chunk_text,
                    "source": source,
                    "page": token_pages[start],
                    "chunk_index": chunk_index,
                    "section_title": title,
                    "section_level": level,
                    "section_index": sec_idx,
                    **meta,
                }
            )
            start += CHUNK_SIZE - CHUNK_OVERLAP
            sub_chunk += 1
            chunk_index += 1

    return chunks


def chunk_document(pages: list[tuple[int, str]], source: str, meta: dict) -> list[dict]:
    """Fixed-stride fallback chunking for PDFs without a usable TOC."""
    all_tokens: list[int] = []
    token_page: list[int] = []

    for page_num, text in pages:
        tokens = enc.encode(text)
        all_tokens.extend(tokens)
        token_page.extend([page_num] * len(tokens))

    chunks = []
    start = 0
    chunk_idx = 0
    while start < len(all_tokens):
        end = min(start + CHUNK_SIZE, len(all_tokens))
        chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{start}"))
        chunks.append(
            {
                "id": chunk_id,
                "text": enc.decode(all_tokens[start:end]),
                "source": source,
                "page": token_page[start],
                "chunk_index": chunk_idx,
                **meta,
            }
        )
        start += CHUNK_SIZE - CHUNK_OVERLAP
        chunk_idx += 1
    return chunks


# ── Markdown chunking ─────────────────────────────────────────────────────────


def chunk_markdown(text: str, source: str, meta: dict) -> list[dict]:
    """Chunk a Markdown document by heading boundaries; fixed-stride fallback."""
    headings = [(m.start(), len(m.group(1)), m.group(2).strip()) for m in HEADING_RE.finditer(text)]

    if not headings:
        return _chunk_text_fixed(text, source, meta)

    raw_sections = []
    for i, (pos, level, title) in enumerate(headings):
        end = headings[i + 1][0] if i + 1 < len(headings) else len(text)
        body = text[pos:end].strip()
        if body:
            raw_sections.append({"title": title, "level": level, "text": body})

    merged: list[dict] = []
    buf_text = ""
    buf_title = ""
    buf_level = 1

    for sec in raw_sections:
        if buf_text:
            buf_text += "\n\n" + sec["text"]
        else:
            buf_text = sec["text"]
            buf_title = sec["title"]
            buf_level = sec["level"]

        if len(enc.encode(buf_text)) >= MIN_SECTION_TOKENS:
            merged.append({"title": buf_title, "level": buf_level, "text": buf_text})
            buf_text = ""

    if buf_text:
        if merged:
            merged[-1]["text"] += "\n\n" + buf_text
        else:
            merged.append({"title": buf_title, "level": buf_level, "text": buf_text})

    chunks: list[dict] = []
    chunk_index = 0

    for sec_idx, sec in enumerate(merged):
        tokens = enc.encode(sec["text"])
        title = sec["title"]
        level = sec["level"]

        sub_chunk = 0
        start = 0
        while start < len(tokens):
            end = min(start + CHUNK_SIZE, len(tokens))
            chunk_text = enc.decode(tokens[start:end])
            if sub_chunk > 0:
                chunk_text = f"{title}\n{chunk_text}"
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{title}:{sub_chunk}"))
            chunks.append(
                {
                    "id": chunk_id,
                    "text": chunk_text,
                    "source": source,
                    "page": 1,
                    "chunk_index": chunk_index,
                    "section_title": title,
                    "section_level": level,
                    "section_index": sec_idx,
                    **meta,
                }
            )
            start += CHUNK_SIZE - CHUNK_OVERLAP
            sub_chunk += 1
            chunk_index += 1

    return chunks


def _chunk_text_fixed(text: str, source: str, meta: dict) -> list[dict]:
    """Fixed-stride chunking for plain text without structural markers."""
    tokens = enc.encode(text)
    chunks = []
    start = 0
    chunk_idx = 0
    while start < len(tokens):
        end = min(start + CHUNK_SIZE, len(tokens))
        chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{start}"))
        chunks.append(
            {
                "id": chunk_id,
                "text": enc.decode(tokens[start:end]),
                "source": source,
                "page": 1,
                "chunk_index": chunk_idx,
                **meta,
            }
        )
        start += CHUNK_SIZE - CHUNK_OVERLAP
        chunk_idx += 1
    return chunks
