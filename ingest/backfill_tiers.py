"""
One-shot backfill: adds trust_tier=1 / source_type=vendor-doc to every chunk
in the collection that is missing these fields.

Run inside the ingest container after upgrading from a pre-trust-tier version:
  docker compose --profile ingest run --rm --entrypoint python ingest backfill_tiers.py

Or via Makefile:  make backfill-tiers
"""

import os

from qdrant_client import QdrantClient
from tqdm import tqdm

QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", 6333))
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "distill")
SCROLL_BATCH = 500
UPSERT_BATCH = 500

qdrant = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)


def main():
    info = qdrant.get_collection(COLLECTION_NAME)
    total = info.points_count
    print(f"Collection '{COLLECTION_NAME}' — {total:,} chunks total")
    print("Scanning for chunks missing trust_tier…")

    ids_to_update: list = []
    offset = None

    with tqdm(total=total, unit="chunks", desc="Scanning") as pbar:
        while True:
            results, offset = qdrant.scroll(
                collection_name=COLLECTION_NAME,
                limit=SCROLL_BATCH,
                offset=offset,
                with_vectors=False,
                with_payload=["trust_tier"],
            )
            for point in results:
                if point.payload.get("trust_tier") is None:
                    ids_to_update.append(point.id)
            pbar.update(len(results))
            if offset is None:
                break

    if not ids_to_update:
        print("\nAll chunks already have trust_tier set — nothing to backfill.")
        return

    print(f"\nBackfilling {len(ids_to_update):,} chunks → trust_tier=1, source_type=vendor-doc …")

    with tqdm(total=len(ids_to_update), unit="chunks", desc="Updating") as pbar:
        for i in range(0, len(ids_to_update), UPSERT_BATCH):
            batch = ids_to_update[i : i + UPSERT_BATCH]
            qdrant.set_payload(
                collection_name=COLLECTION_NAME,
                payload={"trust_tier": 1, "source_type": "vendor-doc"},
                points=batch,
            )
            pbar.update(len(batch))

    print(f"\nDone — {len(ids_to_update):,} chunks updated.")


if __name__ == "__main__":
    main()
