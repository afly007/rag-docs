import argparse
import json
import math
import os
import re
import sys
import time
import uuid
from pathlib import Path

import fitz  # PyMuPDF
import tiktoken
from openai import OpenAI, RateLimitError
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    Modifier,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)
from tqdm import tqdm

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", 6333))
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "network_docs")
DOCS_DIR = Path(os.environ.get("DOCS_DIR", "/docs"))

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
CHUNK_SIZE = 750
CHUNK_OVERLAP = 100
MIN_SECTION_TOKENS = 100  # sections below this are merged forward
EMBED_BATCH = 100
UPSERT_BATCH = 200
PREFETCH_K = 20  # candidates per retriever fed into RRF fusion

# Recognised sidecar keys — any unknown keys are passed through as-is
KNOWN_META_KEYS = {"vendor", "product", "version", "doc_type"}

openai_client = OpenAI(api_key=OPENAI_API_KEY)
qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
enc = tiktoken.get_encoding("cl100k_base")


def compute_sparse(text: str) -> SparseVector:
    counts: dict[int, float] = {}
    for tid in enc.encode(text):
        counts[tid] = counts.get(tid, 0.0) + 1.0
    return SparseVector(indices=list(counts), values=list(counts.values()))


def ensure_collection():
    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION_NAME not in existing:
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config={"dense": VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE)},
            sparse_vectors_config={"bm25": SparseVectorParams(modifier=Modifier.IDF)},
        )
        print(f"Created collection '{COLLECTION_NAME}'")


def delete_source_chunks(source_name: str) -> None:
    """Delete all Qdrant points for source_name before a --force re-ingest."""
    qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=source_name))])
        ),
        wait=True,
    )


def load_sidecar(pdf_path: Path) -> dict:
    """Load optional <filename>.json sidecar with metadata for this PDF."""
    sidecar = pdf_path.with_suffix(".json")
    if not sidecar.exists():
        return {}
    try:
        with open(sidecar) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            print(f"  Warning: {sidecar.name} is not a JSON object — ignoring.")
            return {}
        unknown = set(data) - KNOWN_META_KEYS
        if unknown:
            print(f"  Note: {sidecar.name} contains extra keys {unknown} — storing as-is.")
        return {k: str(v) for k, v in data.items()}  # normalise all values to strings
    except Exception as exc:
        print(f"  Warning: could not read {sidecar.name}: {exc}")
        return {}


def already_ingested(source_name: str) -> bool:
    try:
        results, _ = qdrant.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(
                must=[FieldCondition(key="source", match=MatchValue(value=source_name))]
            ),
            limit=1,
            with_vectors=False,
            with_payload=False,
        )
        return len(results) > 0
    except Exception:
        return False


def extract_toc_sections(doc: fitz.Document) -> list[dict] | None:
    """Extract section boundaries from the PDF's bookmark tree.

    Returns None when the document has no TOC (triggers fixed-stride fallback).
    Each section dict: level, title, start_page (0-indexed), start_y,
    end_page (0-indexed), end_y, has_children.
    """
    toc = doc.get_toc(simple=False)
    if not toc:
        return None

    # Normalise to (level, title, start_page_0idx, start_y)
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
        # End boundary: start of the next entry at the same or higher level
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
            if block[6] == 0:  # text block (type 0); skip images (type 1)
                blocks.append((block[1], block[4]))  # y0, text
        cache.append(blocks)
    return cache


def chunk_document_sections(
    doc: fitz.Document,
    source: str,
    meta: dict,
) -> list[dict] | None:
    """Section-aware chunking using the PDF's TOC as section boundaries.

    Returns None when no TOC is found — caller falls back to chunk_document().
    Chunks carry section_title, section_level, and section_index payload fields.
    """
    sections = extract_toc_sections(doc)
    if sections is None:
        return None

    blocks_cache = _build_page_blocks_cache(doc)
    num_pages = len(blocks_cache)

    # Extract token stream and page-per-token for every leaf section
    section_data: list[dict] = []
    for sec in sections:
        if sec["has_children"]:
            continue  # parent heading; text covered by child sections

        tokens: list[int] = []
        token_pages: list[int] = []

        for pg_idx in range(sec["start_page"], min(sec["end_page"] + 1, num_pages)):
            for y0, text in blocks_cache[pg_idx]:
                # Clip at start boundary (5pt tolerance for exact heading alignment)
                if pg_idx == sec["start_page"] and sec["start_y"] > 5 and y0 < sec["start_y"] - 5:
                    continue
                # Clip at end boundary
                if (
                    pg_idx == sec["end_page"]
                    and sec["end_y"] != float("inf")
                    and y0 >= sec["end_y"]
                ):
                    continue
                block_tokens = enc.encode(text)
                tokens.extend(block_tokens)
                token_pages.extend([pg_idx + 1] * len(block_tokens))  # 1-indexed page

        if tokens:  # skip zero-token sections (header-only entries with no body text)
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

    # Merge consecutive sections that are too short to stand alone
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

    # Flush any remaining short sections onto the last emitted section
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

    # Sliding-window split within each section
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

            # Give later sub-chunks context about which section they belong to
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
    """Chunk the full document as a single token stream.

    Spans page boundaries so CLI syntax that continues across pages stays in
    the same chunk. Records which page each chunk starts on and a sequential
    chunk_index used by the MCP server for context-window expansion.
    """
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
        chunk_text = enc.decode(all_tokens[start:end])
        chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{start}"))
        chunks.append(
            {
                "id": chunk_id,
                "text": chunk_text,
                "source": source,
                "page": token_page[start],
                "chunk_index": chunk_idx,
                **meta,
            }
        )
        start += CHUNK_SIZE - CHUNK_OVERLAP
        chunk_idx += 1
    return chunks


def _retry_after(exc: RateLimitError, default: float = 60.0) -> float:
    """Parse the suggested wait time from an OpenAI rate-limit error message."""
    match = re.search(r"try again in (\d+(?:\.\d+)?)(ms|s)", str(exc))
    if match:
        value, unit = float(match.group(1)), match.group(2)
        return (value / 1000 if unit == "ms" else value) + 1.0
    return default


def embed_chunks(chunks: list[dict]) -> list[dict]:
    texts = [c["text"] for c in chunks]
    embeddings = []
    num_batches = math.ceil(len(texts) / EMBED_BATCH)
    with tqdm(total=num_batches, desc="  Embedding", unit="batch", leave=False) as pbar:
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i : i + EMBED_BATCH]
            while True:
                try:
                    resp = openai_client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
                    embeddings.extend(r.embedding for r in resp.data)
                    pbar.update(1)
                    break
                except RateLimitError as exc:
                    wait = _retry_after(exc)
                    tqdm.write(f"  Rate limit — retrying in {wait:.1f}s…")
                    time.sleep(wait)
    for chunk, vec in zip(chunks, embeddings):
        chunk["vector"] = vec
    return chunks


def ingest_pdf(pdf_path: Path, force: bool = False) -> int:
    file_size_mb = pdf_path.stat().st_size / 1_048_576

    print(f"\n{'─' * 60}")
    print(f"File:  {pdf_path.name}  ({file_size_mb:.1f} MB)")

    if not force and already_ingested(pdf_path.name):
        print("  Already ingested — skipping.  Use --force to re-ingest.")
        return 0

    meta = load_sidecar(pdf_path)
    if meta:
        parts = "  ".join(f"{k}={v}" for k, v in meta.items())
        print(f"Meta:  {parts}")
    else:
        print("Meta:  (none — create a .json sidecar to add vendor/product/version/doc_type)")

    if force:
        delete_source_chunks(pdf_path.name)

    t0 = time.monotonic()

    doc = fitz.open(pdf_path)
    pages = [(i + 1, page.get_text()) for i, page in enumerate(doc)]
    non_empty = [(n, t) for n, t in pages if t.strip()]
    print(f"Pages: {len(pages)} total, {len(non_empty)} with text")

    if not non_empty:
        print("  No extractable text — skipping.")
        return 0

    all_chunks = chunk_document_sections(doc, pdf_path.name, meta)
    if not all_chunks:
        reason = "No TOC detected" if all_chunks is None else "TOC found but no text extracted"
        print(f"  {reason} — using fixed-stride chunking")
        all_chunks = chunk_document(non_empty, pdf_path.name, meta)

    num_batches = math.ceil(len(all_chunks) / EMBED_BATCH)
    print(
        f"Chunks: {len(all_chunks)}  ({num_batches} embedding batch{'es' if num_batches != 1 else ''})"
    )

    all_chunks = embed_chunks(all_chunks)

    for chunk in all_chunks:
        chunk["sparse"] = compute_sparse(chunk["text"])

    # Build payload: fixed fields + optional section fields + all metadata keys
    payload_keys = {
        "text",
        "source",
        "page",
        "chunk_index",
        "section_title",
        "section_level",
        "section_index",
    } | set(meta.keys())
    points = [
        PointStruct(
            id=c["id"],
            vector={"dense": c["vector"], "bm25": c["sparse"]},
            payload={k: c[k] for k in payload_keys if k in c},
        )
        for c in all_chunks
    ]

    num_upsert_batches = math.ceil(len(points) / UPSERT_BATCH)
    with tqdm(total=num_upsert_batches, desc="  Storing ", unit="batch", leave=False) as pbar:
        for i in range(0, len(points), UPSERT_BATCH):
            qdrant.upsert(collection_name=COLLECTION_NAME, points=points[i : i + UPSERT_BATCH])
            pbar.update(1)

    elapsed = time.monotonic() - t0
    print(f"Done:  {len(points)} chunks stored in {elapsed:.1f}s")
    return len(points)


def main():
    parser = argparse.ArgumentParser(description="Ingest PDFs into Qdrant")
    parser.add_argument(
        "files",
        nargs="*",
        help="Specific PDF files to ingest (default: all PDFs under DOCS_DIR)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest files even if already present in the collection",
    )
    args = parser.parse_args()

    ensure_collection()

    targets = [Path(f) for f in args.files] if args.files else list(DOCS_DIR.glob("**/*.pdf"))

    if not targets:
        print(f"No PDFs found in {DOCS_DIR}")
        sys.exit(0)

    print(f"Found {len(targets)} PDF(s) to ingest")
    if args.force:
        print("--force: deleting existing chunks before re-ingesting")

    wall_start = time.monotonic()
    total_chunks = 0
    for pdf in targets:
        total_chunks += ingest_pdf(pdf, force=args.force)

    wall_elapsed = time.monotonic() - wall_start
    print(f"\n{'═' * 60}")
    print(f"Finished: {len(targets)} file(s), {total_chunks} chunks total in {wall_elapsed:.1f}s")


if __name__ == "__main__":
    main()
