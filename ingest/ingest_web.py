"""
Ingest web pages from a JSON manifest into Qdrant as community-tier (tier 4) content.

Manifest format — JSON array of objects, each with at minimum a "url" field:
  [
    {
      "url": "https://example.com/post",
      "vendor": "cisco",
      "product": "ios-xe",
      "doc_type": "config-guide",
      "last_updated": "2024-03-15"
    }
  ]

All entries default to trust_tier=4, source_type=community unless overridden in the manifest.
Run via:  make ingest-web ARGS="/docs/community.json"
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

import trafilatura

from ingest import (
    EMBED_BATCH,
    _upsert_chunks,
    already_ingested,
    chunk_markdown,
    delete_source_chunks,
    ensure_collection,
)

FETCH_TIMEOUT = 30

_DEFAULT_META = {
    "trust_tier": 4,
    "source_type": "community",
}


def fetch_page(url: str) -> str | None:
    downloaded = trafilatura.fetch_url(url)
    if downloaded is None:
        return None
    return trafilatura.extract(
        downloaded,
        include_tables=True,
        include_links=False,
        output_format="markdown",
    )


def ingest_url(url: str, meta: dict, force: bool = False) -> int:
    print(f"\n{'─' * 60}")
    print(f"URL:  {url}")

    if not force and already_ingested(url):
        print("  Already ingested — skipping. Use --force to re-ingest.")
        return 0

    if force:
        delete_source_chunks(url)

    t0 = time.monotonic()
    print("  Fetching … ", end="", flush=True)
    text = fetch_page(url)
    if not text or not text.strip():
        print("no extractable text — skipping.")
        return 0
    print(f"done ({len(text):,} chars)")

    combined_meta = {**_DEFAULT_META, **meta, "url": url}
    all_chunks = chunk_markdown(text, url, combined_meta)

    num_batches = math.ceil(len(all_chunks) / EMBED_BATCH)
    print(
        f"Chunks: {len(all_chunks)}  ({num_batches} embedding batch{'es' if num_batches != 1 else ''})"
    )

    count = _upsert_chunks(all_chunks, combined_meta)
    elapsed = time.monotonic() - t0
    print(f"Done:  {count} chunks stored in {elapsed:.1f}s")
    return count


def load_manifest(path: Path) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"Manifest must be a JSON array, got {type(data).__name__}")
    return data


def _normalise_entry(entry: dict) -> tuple[str, dict]:
    url = entry.get("url", "").strip()
    meta: dict = {}
    for k, v in entry.items():
        if k == "url" or v is None:
            continue
        # Normalise doc_date alias to last_updated
        key = "last_updated" if k == "doc_date" else k
        if key == "trust_tier":
            try:
                meta[key] = int(v)
            except (ValueError, TypeError):
                meta[key] = 4
        else:
            meta[key] = str(v)
    return url, meta


def main():
    parser = argparse.ArgumentParser(
        description="Ingest web pages from a JSON manifest into Qdrant"
    )
    parser.add_argument(
        "manifest",
        help="Path to JSON manifest file (array of objects with 'url' field)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-ingest URLs even if already present in the collection",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        sys.exit(1)

    entries = load_manifest(manifest_path)
    if not entries:
        print("No entries in manifest — nothing to do.")
        sys.exit(0)

    ensure_collection()
    print(f"Found {len(entries)} URL(s) in {manifest_path.name}")
    if args.force:
        print("--force: deleting existing chunks before re-ingesting")

    wall_start = time.monotonic()
    total_chunks = 0

    for entry in entries:
        url, meta = _normalise_entry(entry)
        if not url:
            print("  Skipping entry with no 'url' field")
            continue
        try:
            total_chunks += ingest_url(url, meta, force=args.force)
        except Exception as exc:
            print(f"  Error ingesting {url}: {exc} — skipping")

    wall_elapsed = time.monotonic() - wall_start
    print(f"\n{'═' * 60}")
    print(f"Finished: {len(entries)} URL(s), {total_chunks} chunks total in {wall_elapsed:.1f}s")


if __name__ == "__main__":
    main()
