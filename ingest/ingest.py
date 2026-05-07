import argparse
import json
import math
import os
import sys
import time
import uuid
from pathlib import Path

import fitz  # PyMuPDF
import tiktoken
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
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
EMBED_BATCH = 100
UPSERT_BATCH = 200

# Recognised sidecar keys — any unknown keys are passed through as-is
KNOWN_META_KEYS = {"vendor", "product", "version", "doc_type"}

openai_client = OpenAI(api_key=OPENAI_API_KEY)
qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
enc = tiktoken.get_encoding("cl100k_base")


def ensure_collection():
    existing = {c.name for c in qdrant.get_collections().collections}
    if COLLECTION_NAME not in existing:
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
        print(f"Created collection '{COLLECTION_NAME}'")


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


def extract_pages(pdf_path: Path) -> list[tuple[int, str]]:
    doc = fitz.open(pdf_path)
    return [(i + 1, page.get_text()) for i, page in enumerate(doc)]


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


def embed_chunks(chunks: list[dict]) -> list[dict]:
    texts = [c["text"] for c in chunks]
    embeddings = []
    num_batches = math.ceil(len(texts) / EMBED_BATCH)
    with tqdm(total=num_batches, desc="  Embedding", unit="batch", leave=False) as pbar:
        for i in range(0, len(texts), EMBED_BATCH):
            resp = openai_client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=texts[i : i + EMBED_BATCH],
            )
            embeddings.extend(r.embedding for r in resp.data)
            pbar.update(1)
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

    t0 = time.monotonic()

    pages = extract_pages(pdf_path)
    non_empty = [(n, t) for n, t in pages if t.strip()]
    print(f"Pages: {len(pages)} total, {len(non_empty)} with text")

    if not non_empty:
        print("  No extractable text — skipping.")
        return 0

    all_chunks = chunk_document(non_empty, pdf_path.name, meta)

    num_batches = math.ceil(len(all_chunks) / EMBED_BATCH)
    print(
        f"Chunks: {len(all_chunks)}  ({num_batches} embedding batch{'es' if num_batches != 1 else ''})"
    )

    all_chunks = embed_chunks(all_chunks)

    # Build payload: fixed fields + chunk_index + all metadata keys
    payload_keys = {"text", "source", "page", "chunk_index"} | set(meta.keys())
    points = [
        PointStruct(
            id=c["id"],
            vector=c["vector"],
            payload={k: c[k] for k in payload_keys},
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
        print("--force: skipping duplicate check")

    wall_start = time.monotonic()
    total_chunks = 0
    for pdf in targets:
        total_chunks += ingest_pdf(pdf, force=args.force)

    wall_elapsed = time.monotonic() - wall_start
    print(f"\n{'═' * 60}")
    print(f"Finished: {len(targets)} file(s), {total_chunks} chunks total in {wall_elapsed:.1f}s")


if __name__ == "__main__":
    main()
