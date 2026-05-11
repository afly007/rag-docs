import argparse
import math
import os
import re
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF
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
    SparseVectorParams,
    VectorParams,
)
from tqdm import tqdm

# lib/ is one level up from this file
sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.ingest_core import (  # noqa: E402
    EMBED_BATCH,
    PAYLOAD_KEYS,
    UPSERT_BATCH,
    chunk_document,
    chunk_document_sections,
    chunk_markdown,
    compute_sparse,
    load_sidecar,
)

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", 6333))
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "distill")
DOCS_DIR = Path(os.environ.get("DOCS_DIR", "/docs"))

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536

openai_client = OpenAI(api_key=OPENAI_API_KEY)
qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)


def _source_id(path: Path) -> str:
    try:
        return str(path.relative_to(DOCS_DIR))
    except ValueError:
        return path.name


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
    qdrant.delete(
        collection_name=COLLECTION_NAME,
        points_selector=FilterSelector(
            filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=source_name))])
        ),
        wait=True,
    )


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


def _retry_after(exc: RateLimitError, default: float = 60.0) -> float:
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


def _upsert_chunks(all_chunks: list[dict], meta: dict) -> int:
    all_chunks = embed_chunks(all_chunks)
    for chunk in all_chunks:
        chunk["sparse"] = compute_sparse(chunk["text"])

    payload_keys = PAYLOAD_KEYS | set(meta.keys())
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

    return len(points)


# ── Per-file ingest ───────────────────────────────────────────────────────────


def ingest_pdf(pdf_path: Path, force: bool = False) -> int:
    source = _source_id(pdf_path)
    file_size_mb = pdf_path.stat().st_size / 1_048_576

    print(f"\n{'─' * 60}")
    print(f"File:  {pdf_path.name}  ({file_size_mb:.1f} MB)")

    if not force and already_ingested(source):
        print("  Already ingested — skipping.  Use --force to re-ingest.")
        return 0

    meta = load_sidecar(pdf_path)
    if meta:
        parts = "  ".join(f"{k}={v}" for k, v in meta.items())
        print(f"Meta:  {parts}")
    else:
        print("Meta:  (none — create a .json sidecar to add vendor/product/version/doc_type)")

    if force:
        delete_source_chunks(source)

    t0 = time.monotonic()

    doc = fitz.open(pdf_path)
    pages = [(i + 1, page.get_text()) for i, page in enumerate(doc)]
    non_empty = [(n, t) for n, t in pages if t.strip()]
    print(f"Pages: {len(pages)} total, {len(non_empty)} with text")

    if not non_empty:
        print("  No extractable text — skipping.")
        return 0

    all_chunks = chunk_document_sections(doc, source, meta)
    if not all_chunks:
        reason = "No TOC detected" if all_chunks is None else "TOC found but no text extracted"
        print(f"  {reason} — using fixed-stride chunking")
        all_chunks = chunk_document(non_empty, source, meta)

    num_batches = math.ceil(len(all_chunks) / EMBED_BATCH)
    print(
        f"Chunks: {len(all_chunks)}  ({num_batches} embedding batch{'es' if num_batches != 1 else ''})"
    )

    count = _upsert_chunks(all_chunks, meta)
    elapsed = time.monotonic() - t0
    print(f"Done:  {count} chunks stored in {elapsed:.1f}s")
    return count


def ingest_markdown(md_path: Path, force: bool = False) -> int:
    source = _source_id(md_path)
    file_size_kb = md_path.stat().st_size / 1024

    print(f"\n{'─' * 60}")
    print(f"File:  {md_path.name}  ({file_size_kb:.1f} KB)  [markdown]")

    if not force and already_ingested(source):
        print("  Already ingested — skipping.  Use --force to re-ingest.")
        return 0

    meta = load_sidecar(md_path)
    if meta:
        parts = "  ".join(f"{k}={v}" for k, v in meta.items())
        print(f"Meta:  {parts}")
    else:
        print("Meta:  (none — create a .json sidecar to add source_type/trust_tier/author)")

    if force:
        delete_source_chunks(source)

    t0 = time.monotonic()

    text = md_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        print("  Empty file — skipping.")
        return 0

    all_chunks = chunk_markdown(text, source, meta)
    num_batches = math.ceil(len(all_chunks) / EMBED_BATCH)
    print(
        f"Chunks: {len(all_chunks)}  ({num_batches} embedding batch{'es' if num_batches != 1 else ''})"
    )

    count = _upsert_chunks(all_chunks, meta)
    elapsed = time.monotonic() - t0
    print(f"Done:  {count} chunks stored in {elapsed:.1f}s")
    return count


# ── Watch loop ────────────────────────────────────────────────────────────────


def watch_loop(interval: int = 30) -> None:
    print(f"Watching {DOCS_DIR} for new PDFs and Markdown files (polling every {interval}s) …")
    print("Drop a .pdf or .md into ./docs/ and it will be ingested automatically.\n")
    ensure_collection()
    while True:
        for path in sorted(DOCS_DIR.glob("**/*")):
            suffix = path.suffix.lower()
            if suffix not in (".pdf", ".md"):
                continue
            source = _source_id(path)
            if not already_ingested(source):
                try:
                    if suffix == ".pdf":
                        ingest_pdf(path)
                    else:
                        ingest_markdown(path)
                except Exception as exc:
                    print(f"  Error ingesting {path.name}: {exc} — will retry next cycle")
        time.sleep(interval)


# ── CLI entry point ───────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Ingest PDFs and Markdown files into Qdrant")
    parser.add_argument(
        "files",
        nargs="*",
        help="Specific files to ingest (default: all .pdf and .md files under DOCS_DIR)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest files even if already present in the collection",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Watch DOCS_DIR continuously and ingest new files as they appear",
    )
    args = parser.parse_args()

    if args.watch:
        watch_loop()
        return

    ensure_collection()

    if args.files:
        targets = [Path(f) for f in args.files]
    else:
        targets = sorted(list(DOCS_DIR.glob("**/*.pdf")) + list(DOCS_DIR.glob("**/*.md")))

    if not targets:
        print(f"No PDFs or Markdown files found in {DOCS_DIR}")
        sys.exit(0)

    print(f"Found {len(targets)} file(s) to ingest")
    if args.force:
        print("--force: deleting existing chunks before re-ingesting")

    wall_start = time.monotonic()
    total_chunks = 0
    for path in targets:
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            total_chunks += ingest_pdf(path, force=args.force)
        elif suffix == ".md":
            total_chunks += ingest_markdown(path, force=args.force)
        else:
            print(f"  Skipping unsupported file type: {path.name}")

    wall_elapsed = time.monotonic() - wall_start
    print(f"\n{'═' * 60}")
    print(f"Finished: {len(targets)} file(s), {total_chunks} chunks total in {wall_elapsed:.1f}s")


if __name__ == "__main__":
    main()
