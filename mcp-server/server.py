import asyncio
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import fitz  # PyMuPDF
import uvicorn
from mcp.server.fastmcp import FastMCP
from openai import AsyncOpenAI
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    FieldCondition,
    Filter,
    FilterSelector,
    Fusion,
    FusionQuery,
    MatchAny,
    MatchValue,
    PointStruct,
    Prefetch,
)
from starlette.responses import HTMLResponse, JSONResponse, Response

sys.path.insert(0, str(Path(__file__).parent.parent))
from lib.ingest_core import (  # noqa: E402
    EMBED_BATCH,
    PAYLOAD_KEYS,
    UPSERT_BATCH,
    chunk_document,
    chunk_document_sections,
    chunk_markdown,
    compute_sparse,
    enc,
    load_sidecar,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", 6333))
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "distill")
DB_PATH = os.environ.get("DB_PATH", "/data/queries.db")
EMBEDDING_MODEL = "text-embedding-3-small"
TOP_K = 5
PREFETCH_K = 20  # candidates per retriever fed into RRF fusion
RERANKER = os.environ.get("RERANKER", "").lower()  # "local", "cohere", or ""
RERANKER_CACHE_DIR = os.environ.get("RERANKER_CACHE_DIR", "/data/reranker-cache")
TIER_BOOST_4 = float(os.environ.get("TIER_BOOST_4", "0.75"))
CLIP_API_KEY = os.environ.get("CLIP_API_KEY", "")
DOCS_DIR = Path(os.environ.get("DOCS_DIR", "/docs"))
EMBEDDING_DIM = 1536
STATS_TTL = 60
GAP_THRESHOLD = 0.02  # RRF scores are much smaller than cosine scores
SEARCH_TIMEOUT = 25

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

_TIER_LABELS: dict[int, str] = {
    1: "VENDOR-DOC",
    2: "VALIDATED-DESIGN",
    3: "INTERNAL",
    4: "COMMUNITY",
}

_TIER_ADVISORIES: dict[int, str] = {
    4: "community source — verify before acting",
}


def _tier_badge(tier: int | None) -> str:
    if tier is None:
        return ""
    label = _TIER_LABELS.get(tier, f"tier-{tier}")
    advisory = _TIER_ADVISORIES.get(tier, "")
    badge = f"[{label} tier-{tier}]"
    if advisory:
        badge += f" — {advisory}"
    return badge


def _tier_preamble(hits: list) -> str | None:
    tiers_seen: dict[int, int] = {}
    for hit in hits:
        tier = hit.payload.get("trust_tier") or 1
        tiers_seen[tier] = tiers_seen.get(tier, 0) + 1
    if len(tiers_seen) <= 1:
        return None
    parts = []
    for tier in sorted(tiers_seen):
        label = _TIER_LABELS.get(tier, f"tier-{tier}")
        count = tiers_seen[tier]
        advisory = f" ({_TIER_ADVISORIES[tier]})" if tier in _TIER_ADVISORIES else ""
        parts.append(f"  {count}× {label} tier-{tier}{advisory}")
    return "Results span multiple source tiers:\n" + "\n".join(parts)


def apply_tier_boost(hits: list) -> list:
    """Re-sort hits applying a score penalty to tier-4 community results."""
    if TIER_BOOST_4 >= 1.0:
        return hits

    def boosted(hit):
        if hit.payload.get("trust_tier") == 4:
            return hit.score * TIER_BOOST_4
        return hit.score

    return sorted(hits, key=boosted, reverse=True)


# ── Qdrant ────────────────────────────────────────────────────────────────────


def connect_qdrant(retries: int = 10, delay: float = 2.0) -> QdrantClient:
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    for _ in range(retries):
        try:
            client.get_collections()
            log.info("Connected to Qdrant")
            return client
        except Exception as exc:
            log.warning("Qdrant not ready (%s), retrying in %.0fs…", exc, delay)
            time.sleep(delay)
    raise RuntimeError("Could not connect to Qdrant after multiple retries")


# Vendor aliases — any name in a group matches all names in that group
_VENDOR_ALIASES: dict[str, list[str]] = {
    "aruba": ["aruba", "hewlett-packard-enterprise", "hpe", "arubanetworks"],
    "hpe": ["aruba", "hewlett-packard-enterprise", "hpe", "arubanetworks"],
    "hewlett-packard-enterprise": ["aruba", "hewlett-packard-enterprise", "hpe", "arubanetworks"],
    "arubanetworks": ["aruba", "hewlett-packard-enterprise", "hpe", "arubanetworks"],
}


def build_filter(
    vendor: str,
    product: str,
    doc_type: str,
    version: str,
    source_type: str = "",
    community: bool = False,
) -> Filter:
    """Build a Qdrant Filter.

    By default excludes trust_tier=4 (community) content.
    When community=True, restricts to ONLY trust_tier=4 content.
    """
    conditions = []
    if vendor:
        aliases = _VENDOR_ALIASES.get(vendor.lower(), [vendor])
        conditions.append(FieldCondition(key="vendor", match=MatchAny(any=aliases)))
    if product:
        conditions.append(FieldCondition(key="product", match=MatchValue(value=product)))
    if doc_type:
        conditions.append(FieldCondition(key="doc_type", match=MatchValue(value=doc_type)))
    if version:
        conditions.append(FieldCondition(key="version", match=MatchValue(value=version)))
    if source_type:
        conditions.append(FieldCondition(key="source_type", match=MatchValue(value=source_type)))

    if community:
        conditions.append(FieldCondition(key="trust_tier", match=MatchValue(value=4)))
        return Filter(must=conditions)

    return Filter(
        must=conditions,
        must_not=[FieldCondition(key="trust_tier", match=MatchValue(value=4))],
    )


qdrant = connect_qdrant()
mcp = FastMCP("distill", host="0.0.0.0", port=8000)

# ── Re-ranker ─────────────────────────────────────────────────────────────────

_reranker = None


def init_reranker() -> None:
    global _reranker
    if RERANKER == "local":
        from flashrank import Ranker

        log.info("Loading local re-ranker (ms-marco-MiniLM-L-12-v2)…")
        _reranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2", cache_dir=RERANKER_CACHE_DIR)
        log.info("Local re-ranker ready")
    elif RERANKER == "cohere":
        import cohere

        api_key = os.environ.get("COHERE_API_KEY", "")
        if not api_key:
            raise RuntimeError("RERANKER=cohere requires COHERE_API_KEY to be set")
        _reranker = cohere.Client(api_key=api_key)
        log.info("Cohere re-ranker configured")
    else:
        log.info(
            "Re-ranking disabled (RERANKER=%r — set to 'local' or 'cohere' to enable)", RERANKER
        )


def rerank_hits(query: str, hits: list) -> list:
    """Re-rank hits with the configured backend; returns hits[:TOP_K] in new order."""
    if _reranker is None or not hits:
        return hits[:TOP_K]

    if RERANKER == "local":
        from flashrank import RerankRequest

        passages = [{"id": i, "text": h.payload["text"]} for i, h in enumerate(hits)]
        results = _reranker.rerank(RerankRequest(query=query, passages=passages))
        return [hits[r["id"]] for r in results[:TOP_K]]

    if RERANKER == "cohere":
        texts = [h.payload["text"] for h in hits]
        resp = _reranker.rerank(
            query=query,
            documents=texts,
            model="rerank-english-v3.0",
            top_n=TOP_K,
        )
        return [hits[r.index] for r in resp.results]

    return hits[:TOP_K]


# ── Query log (SQLite) ────────────────────────────────────────────────────────

_db_lock = threading.Lock()
_active_lock = threading.Lock()
_active: dict = {}
_id_counter = 0


def _next_id() -> int:
    global _id_counter
    with _active_lock:
        _id_counter += 1
        return _id_counter


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS queries (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT    NOT NULL,
                query           TEXT    NOT NULL,
                vendor          TEXT,
                product         TEXT,
                doc_type        TEXT,
                top_score       REAL,
                result_count    INTEGER,
                top_source      TEXT,
                top_page        INTEGER,
                latency_ms      INTEGER,
                top_source_type TEXT
            )
        """)
        # Migrate: add columns absent from older schema versions
        existing = {row[1] for row in conn.execute("PRAGMA table_info(queries)")}
        for col in ("vendor", "product", "doc_type", "top_source_type"):
            if col not in existing:
                conn.execute(f"ALTER TABLE queries ADD COLUMN {col} TEXT")
        conn.commit()


def log_query(
    query: str,
    vendor: str,
    product: str,
    doc_type: str,
    top_score: float | None,
    result_count: int,
    top_source: str | None,
    top_page: int | None,
    latency_ms: int,
    top_source_type: str | None = None,
):
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _db_lock, sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO queries "
                "(ts, query, vendor, product, doc_type, top_score, result_count, "
                "top_source, top_page, latency_ms, top_source_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    query,
                    vendor or None,
                    product or None,
                    doc_type or None,
                    top_score,
                    result_count,
                    top_source,
                    top_page,
                    latency_ms,
                    top_source_type,
                ),
            )
            conn.commit()
    except Exception as exc:
        log.warning("Failed to log query: %s", exc)


def query_db(sql: str, params: tuple = ()) -> list[tuple]:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        return conn.execute(sql, params).fetchall()


# ── Shared search helper ───────────────────────────────────────────────────────


async def _run_search(query: str, query_filter: Filter) -> list:
    """Hybrid search + optional rerank. Returns up to TOP_K hits."""
    resp = await openai_client.embeddings.create(model=EMBEDDING_MODEL, input=query)
    query_vector = resp.data[0].embedding

    qdrant_limit = PREFETCH_K if _reranker else TOP_K
    result = qdrant.query_points(
        collection_name=COLLECTION_NAME,
        prefetch=[
            Prefetch(query=query_vector, using="dense", limit=PREFETCH_K),
            Prefetch(query=compute_sparse(query), using="bm25", limit=PREFETCH_K),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        query_filter=query_filter,
        limit=qdrant_limit,
        with_payload=True,
    )
    return rerank_hits(query, result.points)


# ── MCP tools ─────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_docs() -> str:
    """
    List all ingested documents with their metadata (vendor, product, version, doc_type,
    source_type, trust tier) and the filter values accepted by search_docs(). Call this
    first to discover what is available before filtering a search.
    """
    stats = collect_qdrant_stats()
    if "error" in stats:
        return f"Error: {stats['error']}"
    if not stats["sources"]:
        return "No documents ingested yet."

    vendors = sorted({v for s in stats["sources"].values() if (v := s.get("vendor"))})
    products = sorted({v for s in stats["sources"].values() if (v := s.get("product"))})
    versions = sorted({v for s in stats["sources"].values() if (v := s.get("version"))})
    doc_types = sorted({v for s in stats["sources"].values() if (v := s.get("doc_type"))})
    source_types = sorted({v for s in stats["sources"].values() if (v := s.get("source_type"))})

    lines = [
        f"Collection: {stats['collection']}",
        f"Documents:  {stats['total_docs']}   Chunks: {stats['total_chunks']:,}",
        "",
        f"Available vendors:      {', '.join(vendors) or '(none tagged)'}",
        f"Available products:     {', '.join(products) or '(none tagged)'}",
        f"Available versions:     {', '.join(versions) or '(none tagged)'}",
        f"Available doc_types:    {', '.join(doc_types) or '(none tagged)'}",
        f"Available source_types: {', '.join(source_types) or '(none tagged)'}",
        "",
        f"{'Document':<45} {'Vendor':<12} {'Product':<12} {'Version':<10} {'Doc Type':<18} {'Tier':<6} Chunks",
        "─" * 118,
    ]
    for src, info in sorted(stats["sources"].items()):
        tier = info.get("trust_tier")
        tier_str = str(tier) if tier is not None else "—"
        lines.append(
            f"{src:<45} "
            f"{info.get('vendor') or '—':<12} "
            f"{info.get('product') or '—':<12} "
            f"{info.get('version') or '—':<10} "
            f"{info.get('doc_type') or '—':<18} "
            f"{tier_str:<6} "
            f"{info['chunks']:,}"
        )

    lines += [
        "",
        "Trust tiers: 1=vendor-doc (authoritative)  2=validated-design  3=internal  4=community",
        "Community (tier-4) docs are excluded from search_docs() — use search_community() to query them.",
        "",
        "Use search_docs(query, vendor=..., product=..., doc_type=..., version=..., source_type=...) to filter.",
        "Untagged documents are always included in unfiltered searches.",
    ]
    return "\n".join(lines)


def _fetch_adjacent_text(source: str, chunk_index: int) -> tuple[str | None, str | None]:
    """Return (prev_text, next_text) for the neighbouring chunks in the same document."""
    prev_text = next_text = None
    for offset, slot in ((-1, "prev"), (1, "next")):
        target_idx = chunk_index + offset
        if target_idx < 0:
            continue
        try:
            results, _ = qdrant.scroll(
                collection_name=COLLECTION_NAME,
                scroll_filter=Filter(
                    must=[
                        FieldCondition(key="source", match=MatchValue(value=source)),
                        FieldCondition(key="chunk_index", match=MatchValue(value=target_idx)),
                    ]
                ),
                limit=1,
                with_vectors=False,
                with_payload=["text"],
            )
            if results:
                text = results[0].payload.get("text", "")
                if slot == "prev":
                    prev_text = text
                else:
                    next_text = text
        except Exception:
            pass
    return prev_text, next_text


def _build_context_block(match_text: str, prev_text: str | None, next_text: str | None) -> str:
    """Wrap the matched chunk with optional preceding/following context."""
    parts = []
    if prev_text:
        parts.append(f"[preceding context]\n{prev_text.strip()}")
    parts.append(f"[matched section]\n{match_text.strip()}")
    if next_text:
        parts.append(f"[following context]\n{next_text.strip()}")
    return "\n\n".join(parts)


@mcp.tool()
async def search_docs(
    query: str,
    vendor: str = "",
    product: str = "",
    doc_type: str = "",
    version: str = "",
    source_type: str = "",
) -> str:
    """
    Search ingested vendor documentation for the most relevant sections.
    Covers vendor-official (tier 1), validated-design (tier 2), and internal (tier 3) content.
    Community sources (tier 4) are excluded — use search_community() for those.

    Returns the top 5 matching chunks with source, page, relevance score, and text.
    Use list_docs() first to see available filter values.

    Args:
        query:       What to search for.
        vendor:      Optional — filter to a specific vendor (e.g. "cisco", "juniper").
        product:     Optional — filter to a specific product (e.g. "ios-xe", "junos").
        doc_type:    Optional — filter by document type (e.g. "cli-reference", "validated-design").
        version:     Optional — filter to a specific version (e.g. "10.16", "17.9.1").
        source_type: Optional — filter by source type ("vendor-doc", "validated-design", "internal").
    """
    filter_desc = "  ".join(
        f"{k}={v}"
        for k, v in [
            ("vendor", vendor),
            ("product", product),
            ("doc_type", doc_type),
            ("version", version),
            ("source_type", source_type),
        ]
        if v
    )
    log.info(
        "search_docs query=%r  filters: %s  reranker: %s",
        query,
        filter_desc or "none",
        RERANKER or "off",
    )

    t0 = time.monotonic()
    qid = _next_id()
    with _active_lock:
        _active[qid] = {
            "query": query + (f"  [{filter_desc}]" if filter_desc else ""),
            "started_at": t0,
            "started_ts": datetime.now(UTC).strftime("%H:%M:%S UTC"),
        }

    try:
        async with asyncio.timeout(SEARCH_TIMEOUT):
            try:
                hits = await _run_search(
                    query,
                    build_filter(vendor, product, doc_type, version, source_type),
                )
                hits = apply_tier_boost(hits)
            except UnexpectedResponse as exc:
                if "doesn't exist" in str(exc):
                    return "No documents have been ingested yet."
                raise

        latency_ms = int((time.monotonic() - t0) * 1000)

        if not hits:
            no_result_msg = "No relevant documentation found."
            if vendor or product or doc_type or source_type:
                no_result_msg += (
                    f" Filters applied: {filter_desc}. "
                    "Try calling list_docs() to verify filter values, or search without filters."
                )
            log_query(query, vendor, product, doc_type, None, 0, None, None, latency_ms)
            return no_result_msg

        top = hits[0]
        log_query(
            query,
            vendor,
            product,
            doc_type,
            round(top.score, 4),
            len(hits),
            top.payload.get("source"),
            top.payload.get("page"),
            latency_ms,
            top_source_type=top.payload.get("source_type"),
        )

    except TimeoutError:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.error("search_docs timed out after %dms for query=%r", latency_ms, query)
        log_query(query, vendor, product, doc_type, None, 0, None, None, latency_ms)
        return f"Search timed out after {SEARCH_TIMEOUT}s. Try again."

    finally:
        with _active_lock:
            _active.pop(qid, None)

    sections = []
    preamble = _tier_preamble(hits)
    if preamble:
        sections.append(preamble)

    for i, hit in enumerate(hits, 1):
        p = hit.payload
        meta_parts = "  ".join(
            f"{k}={p[k]}" for k in ("vendor", "product", "version", "doc_type") if p.get(k)
        )
        header = f"[{i}] {p['source']}  |  page {p.get('page', '?')}  |  score {hit.score:.3f}"
        if meta_parts:
            header += f"  |  {meta_parts}"
        if section := p.get("section_title"):
            header += f"  |  §{section}"
        if url := p.get("url"):
            header += f"  |  {url}"
        if badge := _tier_badge(p.get("trust_tier")):
            header += f"  |  {badge}"

        chunk_idx = p.get("chunk_index")
        if chunk_idx is not None:
            prev_text, next_text = _fetch_adjacent_text(p["source"], chunk_idx)
        else:
            prev_text = next_text = None

        body = _build_context_block(p["text"], prev_text, next_text)
        sections.append(f"{header}\n{body}")

    return "\n\n---\n\n".join(sections)


@mcp.tool()
async def search_community(
    query: str,
    vendor: str = "",
    product: str = "",
    doc_type: str = "",
) -> str:
    """
    Search community-sourced references (curated Reddit posts, blog articles, web pages).
    These are tier-4 sources — useful for real-world context and peer experience,
    but NOT authoritative. Always verify findings against vendor documentation before acting.

    Requires community content to have been ingested via `make ingest-web`.

    Args:
        query:    What to search for.
        vendor:   Optional — narrow to a specific vendor.
        product:  Optional — narrow to a specific product.
        doc_type: Optional — filter by document type.
    """
    filter_desc = "  ".join(
        f"{k}={v}"
        for k, v in [("vendor", vendor), ("product", product), ("doc_type", doc_type)]
        if v
    )
    log.info("search_community query=%r  filters: %s", query, filter_desc or "none")

    t0 = time.monotonic()
    qid = _next_id()
    with _active_lock:
        _active[qid] = {
            "query": f"[community] {query}" + (f"  [{filter_desc}]" if filter_desc else ""),
            "started_at": t0,
            "started_ts": datetime.now(UTC).strftime("%H:%M:%S UTC"),
        }

    try:
        async with asyncio.timeout(SEARCH_TIMEOUT):
            try:
                hits = await _run_search(
                    query,
                    build_filter(vendor, product, doc_type, "", community=True),
                )
            except UnexpectedResponse as exc:
                if "doesn't exist" in str(exc):
                    return "No documents have been ingested yet."
                raise

        latency_ms = int((time.monotonic() - t0) * 1000)

        if not hits:
            msg = "No community references found."
            if vendor or product or doc_type:
                msg += f" Filters applied: {filter_desc}."
            log_query(
                query, vendor, product, doc_type, None, 0, None, None, latency_ms, "community"
            )
            return msg

        top = hits[0]
        log_query(
            query,
            vendor,
            product,
            doc_type,
            round(top.score, 4),
            len(hits),
            top.payload.get("source"),
            top.payload.get("page"),
            latency_ms,
            top_source_type="community",
        )

    except TimeoutError:
        latency_ms = int((time.monotonic() - t0) * 1000)
        log.error("search_community timed out after %dms for query=%r", latency_ms, query)
        log_query(query, vendor, product, doc_type, None, 0, None, None, latency_ms)
        return f"Search timed out after {SEARCH_TIMEOUT}s. Try again."

    finally:
        with _active_lock:
            _active.pop(qid, None)

    caveat = (
        "COMMUNITY SOURCES — tier 4. Results from curated community content "
        "(blogs, forum posts, web articles). May contain useful real-world experience "
        "but NOT authoritative. Verify against vendor documentation before implementing."
    )

    sections = [caveat]
    for i, hit in enumerate(hits, 1):
        p = hit.payload
        meta_parts = "  ".join(f"{k}={p[k]}" for k in ("vendor", "product", "version") if p.get(k))
        header = f"[{i}] score {hit.score:.3f}"
        if meta_parts:
            header += f"  |  {meta_parts}"
        if section := p.get("section_title"):
            header += f"  |  §{section}"
        if url := p.get("url"):
            header += f"\n    URL: {url}"
        if doc_date := p.get("last_updated"):
            header += f"  |  date: {doc_date}"

        sections.append(f"{header}\n{p['text'].strip()}")

    return "\n\n---\n\n".join(sections)


# ── Browser clipper ───────────────────────────────────────────────────────────

_CLIP_HEADING_RE = re.compile(r"^#{1,3}\s+.+$", re.MULTILINE)
_CLIP_CHUNK_SIZE = 750
_CLIP_CHUNK_OVERLAP = 100


def _clip_fetch(url: str) -> str | None:
    """Fetch and extract main text from a URL. Runs in a thread pool (sync)."""
    import trafilatura

    downloaded = trafilatura.fetch_url(url)
    if not downloaded:
        return None

    text = trafilatura.extract(
        downloaded,
        include_tables=True,
        include_links=False,
        output_format="markdown",
    )
    if text:
        return text

    # Fallback: lenient extraction (include everything trafilatura finds)
    text = trafilatura.extract(
        downloaded,
        include_tables=True,
        include_links=False,
        include_comments=True,
        no_fallback=False,
        favor_recall=True,
        output_format="markdown",
    )
    if text:
        return text

    # Last resort: strip HTML tags and return raw visible text
    import re as _re

    raw = _re.sub(r"<[^>]+>", " ", downloaded)
    raw = _re.sub(r"[ \t]{2,}", " ", raw).strip()
    return raw if len(raw) > 200 else None


def _clip_chunk(text: str, source: str, meta: dict) -> list[dict]:
    """Heading-boundary chunking for web content; fixed-stride fallback."""
    positions = [m.start() for m in _CLIP_HEADING_RE.finditer(text)]
    sections: list[str] = []
    if positions:
        # Capture any content before the first heading
        if positions[0] > 0:
            preamble = text[: positions[0]].strip()
            if preamble:
                sections.append(preamble)
        for i, pos in enumerate(positions):
            end = positions[i + 1] if i + 1 < len(positions) else len(text)
            body = text[pos:end].strip()
            if body:
                sections.append(body)
    else:
        sections = [text.strip()]

    chunks: list[dict] = []
    chunk_idx = 0
    for section in sections:
        tokens = enc.encode(section)
        start = 0
        while start < len(tokens):
            end = min(start + _CLIP_CHUNK_SIZE, len(tokens))
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}:{chunk_idx}"))
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
            start += _CLIP_CHUNK_SIZE - _CLIP_CHUNK_OVERLAP
            chunk_idx += 1
    return chunks


@mcp.custom_route("/clip/meta", methods=["GET", "OPTIONS"])
async def clip_meta_handler(request):
    """Return distinct vendor and product values from the collection for the extension dropdowns."""
    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors)

    auth = request.headers.get("Authorization", "")
    if CLIP_API_KEY and auth != f"Bearer {CLIP_API_KEY}":
        return JSONResponse({"error": "Unauthorized"}, status_code=401, headers=cors)

    vendors: set[str] = set()
    products: set[str] = set()
    offset = None
    while True:
        result = qdrant.scroll(
            collection_name=COLLECTION_NAME,
            limit=1000,
            offset=offset,
            with_payload=["vendor", "product"],
            with_vectors=False,
        )
        for point in result[0]:
            if v := point.payload.get("vendor"):
                vendors.add(v)
            if p := point.payload.get("product"):
                products.add(p)
        offset = result[1]
        if offset is None:
            break

    return JSONResponse(
        {"vendors": sorted(vendors), "products": sorted(products)},
        headers=cors,
    )


@mcp.custom_route("/clip", methods=["POST", "OPTIONS"])
async def clip_handler(request):
    """Receive a URL from the browser extension, fetch it, and ingest it as tier-4 community content."""
    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }

    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors)

    if not CLIP_API_KEY:
        return JSONResponse(
            {"error": "Clipper not configured — set CLIP_API_KEY in your server environment."},
            status_code=503,
            headers=cors,
        )

    auth = request.headers.get("Authorization", "")
    if not (auth.startswith("Bearer ") and auth[7:] == CLIP_API_KEY):
        return JSONResponse({"error": "Unauthorized"}, status_code=401, headers=cors)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400, headers=cors)

    url = (body.get("url") or "").strip()
    if not url:
        return JSONResponse({"error": "'url' is required"}, status_code=400, headers=cors)

    # Skip if already ingested
    try:
        existing, _ = qdrant.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=url))]),
            limit=1,
            with_vectors=False,
            with_payload=False,
        )
        if existing:
            return JSONResponse(
                {"chunks": 0, "skipped": True, "message": "Already ingested"},
                headers=cors,
            )
    except Exception:
        pass

    meta: dict = {"trust_tier": 4, "source_type": "community", "url": url}
    for key in ("vendor", "product", "doc_type"):
        if val := (body.get(key) or "").strip().lower():
            meta[key] = val
    if val := (body.get("last_updated") or "").strip():
        meta["last_updated"] = val

    log.info("clip url=%r  meta=%s", url, {k: v for k, v in meta.items() if k != "url"})

    try:
        async with asyncio.timeout(60):
            text = await asyncio.get_event_loop().run_in_executor(None, _clip_fetch, url)
            if not text or not text.strip():
                return JSONResponse(
                    {"error": "No extractable text found at that URL"},
                    status_code=422,
                    headers=cors,
                )

            chunks = _clip_chunk(text, url, meta)

            # Embed in batches of 100
            texts = [c["text"] for c in chunks]
            all_embeddings: list = []
            for i in range(0, len(texts), 100):
                resp = await openai_client.embeddings.create(
                    model=EMBEDDING_MODEL, input=texts[i : i + 100]
                )
                all_embeddings.extend(r.embedding for r in resp.data)

            for chunk, vec in zip(chunks, all_embeddings):
                chunk["vector"] = vec
                chunk["sparse"] = compute_sparse(chunk["text"])

            points = [
                PointStruct(
                    id=c["id"],
                    vector={"dense": c["vector"], "bm25": c["sparse"]},
                    payload={k: c[k] for k in c if k not in ("id", "vector", "sparse")},
                )
                for c in chunks
            ]
            for i in range(0, len(points), 200):
                qdrant.upsert(collection_name=COLLECTION_NAME, points=points[i : i + 200])

            _qdrant_cache["at"] = 0.0  # invalidate stats cache

    except TimeoutError:
        return JSONResponse(
            {"error": "Timed out fetching the page — it may be too slow or require a login"},
            status_code=504,
            headers=cors,
        )
    except UnexpectedResponse as exc:
        if "doesn't exist" in str(exc):
            return JSONResponse(
                {"error": "Collection not found — run `make ingest` once to create it"},
                status_code=503,
                headers=cors,
            )
        return JSONResponse({"error": str(exc)}, status_code=500, headers=cors)
    except Exception as exc:
        log.error("clip error url=%r: %s", url, exc)
        return JSONResponse({"error": str(exc)}, status_code=500, headers=cors)

    log.info("clip done url=%r  chunks=%d", url, len(chunks))
    return JSONResponse({"chunks": len(chunks), "source": url}, headers=cors)


# ── File browser ─────────────────────────────────────────────────────────────

_ingest_jobs: dict[str, dict] = {}  # job_id -> {status, file, chunks, error}
_ingest_jobs_lock = threading.Lock()
_SUPPORTED_SUFFIXES = {".pdf", ".md"}


def _file_source_id(path: Path) -> str:
    try:
        return str(path.relative_to(DOCS_DIR))
    except ValueError:
        return path.name


def _safe_path(raw: str) -> Path | None:
    """Resolve a relative path under DOCS_DIR; return None if it escapes the root."""
    try:
        resolved = (DOCS_DIR / raw).resolve()
        resolved.relative_to(DOCS_DIR.resolve())
        return resolved
    except (ValueError, Exception):
        return None


async def _embed_chunks_async(chunks: list[dict]) -> list[dict]:
    texts = [c["text"] for c in chunks]
    all_embeddings: list = []
    for i in range(0, len(texts), EMBED_BATCH):
        resp = await openai_client.embeddings.create(
            model=EMBEDDING_MODEL, input=texts[i : i + EMBED_BATCH]
        )
        all_embeddings.extend(r.embedding for r in resp.data)
    for chunk, vec in zip(chunks, all_embeddings):
        chunk["vector"] = vec
    return chunks


async def _ingest_file_bg(path: Path, job_id: str) -> None:
    """Ingest a single PDF or Markdown file; always force-replaces existing chunks."""
    with _ingest_jobs_lock:
        _ingest_jobs[job_id] = {"status": "running", "file": path.name, "chunks": 0, "error": ""}
    try:
        source = _file_source_id(path)
        meta = load_sidecar(path)

        # Purge existing chunks for this source before re-indexing
        qdrant.delete(
            collection_name=COLLECTION_NAME,
            points_selector=FilterSelector(
                filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))])
            ),
            wait=True,
        )

        suffix = path.suffix.lower()
        if suffix == ".pdf":
            doc = fitz.open(str(path))
            pages = [(i + 1, page.get_text()) for i, page in enumerate(doc)]
            non_empty = [(n, t) for n, t in pages if t.strip()]
            if not non_empty:
                raise ValueError("No extractable text found in PDF")
            all_chunks = chunk_document_sections(doc, source, meta)
            if not all_chunks:
                all_chunks = chunk_document(non_empty, source, meta)
        elif suffix == ".md":
            text = path.read_text(encoding="utf-8", errors="replace")
            if not text.strip():
                raise ValueError("File is empty")
            all_chunks = chunk_markdown(text, source, meta)
        else:
            raise ValueError(f"Unsupported file type: {path.suffix}")

        all_chunks = await _embed_chunks_async(all_chunks)
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
        for i in range(0, len(points), UPSERT_BATCH):
            qdrant.upsert(collection_name=COLLECTION_NAME, points=points[i : i + UPSERT_BATCH])

        _qdrant_cache["at"] = 0.0
        with _ingest_jobs_lock:
            _ingest_jobs[job_id] = {
                "status": "done",
                "file": path.name,
                "chunks": len(points),
                "error": "",
            }
        log.info("file ingest done path=%r chunks=%d", str(path), len(points))

    except Exception as exc:
        log.error("file ingest error path=%r: %s", str(path), exc)
        with _ingest_jobs_lock:
            _ingest_jobs[job_id] = {
                "status": "error",
                "file": path.name,
                "chunks": 0,
                "error": str(exc),
            }


def _list_docs_files() -> list[dict]:
    if not DOCS_DIR.exists():
        return []
    stats = collect_qdrant_stats()
    sources = stats.get("sources", {})
    files = []
    for path in sorted(DOCS_DIR.glob("**/*")):
        if path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            continue
        rel = _file_source_id(path)
        st = path.stat()
        sidecar = load_sidecar(path)
        files.append(
            {
                "name": path.name,
                "path": rel,
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
                "type": path.suffix.lower().lstrip("."),
                "chunks": sources.get(rel, {}).get("chunks", 0),
                "has_sidecar": path.with_suffix(".json").exists(),
                "meta": sidecar,
            }
        )
    return files


@mcp.custom_route("/files", methods=["GET"])
async def files_page_handler(request):
    return HTMLResponse(_render_files_page())


@mcp.custom_route("/files/list", methods=["GET"])
async def files_list_handler(request):
    return JSONResponse(_list_docs_files())


@mcp.custom_route("/files/upload", methods=["POST"])
async def files_upload_handler(request):
    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"error": "Expected multipart/form-data"}, status_code=400)

    file = form.get("file")
    if file is None:
        return JSONResponse({"error": "No file provided"}, status_code=400)

    filename = Path(file.filename).name  # strip any path components
    if Path(filename).suffix.lower() not in _SUPPORTED_SUFFIXES:
        return JSONResponse({"error": "Only .pdf and .md files are supported"}, status_code=400)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    dest = DOCS_DIR / filename
    content = await file.read()
    dest.write_bytes(content)
    log.info("file uploaded path=%r size=%d", str(dest), len(content))

    job_id = str(uuid.uuid4())
    asyncio.create_task(_ingest_file_bg(dest, job_id))
    return JSONResponse({"job_id": job_id, "file": filename})


@mcp.custom_route("/files/status", methods=["GET"])
async def files_status_handler(request):
    job_id = request.query_params.get("job_id", "")
    with _ingest_jobs_lock:
        job = _ingest_jobs.get(job_id)
    if not job:
        return JSONResponse({"error": "Unknown job"}, status_code=404)
    return JSONResponse(job)


@mcp.custom_route("/files/delete", methods=["DELETE", "POST"])
async def files_delete_handler(request):
    rel = request.query_params.get("path", "")
    if not rel:
        return JSONResponse({"error": "'path' query param required"}, status_code=400)

    target = _safe_path(rel)
    if target is None or not target.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    source = _file_source_id(target)

    # Purge chunks from Qdrant
    try:
        qdrant.delete(
            collection_name=COLLECTION_NAME,
            points_selector=FilterSelector(
                filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))])
            ),
            wait=True,
        )
    except Exception as exc:
        log.warning("qdrant delete error for %r: %s", source, exc)

    # Remove file and sidecar
    target.unlink(missing_ok=True)
    sidecar = target.with_suffix(".json")
    sidecar.unlink(missing_ok=True)

    _qdrant_cache["at"] = 0.0
    log.info("file deleted path=%r", str(target))
    return JSONResponse({"deleted": rel})


@mcp.custom_route("/files/download", methods=["GET"])
async def files_download_handler(request):
    rel = request.query_params.get("path", "")
    if not rel:
        return JSONResponse({"error": "'path' query param required"}, status_code=400)

    target = _safe_path(rel)
    if target is None or not target.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    content = target.read_bytes()
    media = "application/pdf" if target.suffix.lower() == ".pdf" else "text/markdown"
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{target.name}"'},
    )


@mcp.custom_route("/files/sidecar", methods=["GET", "PUT"])
async def files_sidecar_handler(request):
    rel = request.query_params.get("path", "")
    if not rel:
        return JSONResponse({"error": "'path' query param required"}, status_code=400)

    target = _safe_path(rel)
    if target is None:
        return JSONResponse({"error": "Invalid path"}, status_code=400)

    if request.method == "GET":
        sidecar = target.with_suffix(".json")
        if sidecar.exists():
            return JSONResponse(json.loads(sidecar.read_text()))
        return JSONResponse({})

    # PUT — save sidecar and optionally re-ingest
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "Body must be a JSON object"}, status_code=400)

    # Strip empty values
    cleaned = {k: v for k, v in body.items() if v not in (None, "")}

    sidecar = target.with_suffix(".json")
    sidecar.write_text(json.dumps(cleaned, indent=2))
    log.info("sidecar saved path=%r", str(sidecar))

    reingest = request.query_params.get("reingest", "").lower() in ("1", "true", "yes")
    if reingest and target.exists():
        job_id = str(uuid.uuid4())
        asyncio.create_task(_ingest_file_bg(target, job_id))
        return JSONResponse({"saved": True, "job_id": job_id})

    return JSONResponse({"saved": True})


def _render_files_page() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Distill — Files</title>
  <meta http-equiv="refresh" content="0; url=/files#">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: ui-monospace, "Cascadia Code", "Fira Mono", monospace;
           background: #0d1117; color: #c9d1d9; min-height: 100vh; }

    header { display: flex; align-items: center; gap: 16px; padding: 14px 24px;
             border-bottom: 1px solid #21262d; }
    header h1 { font-size: .9rem; color: #58a6ff; letter-spacing: .04em; flex: 1; }
    header nav a { font-size: .75rem; color: #484f58; text-decoration: none;
                   padding: 4px 10px; border: 1px solid #30363d; border-radius: 6px; }
    header nav a:hover { color: #c9d1d9; border-color: #58a6ff; }

    main { max-width: 1100px; margin: 0 auto; padding: 24px; }

    /* Upload zone */
    #drop-zone { border: 2px dashed #30363d; border-radius: 8px; padding: 32px;
                 text-align: center; color: #484f58; cursor: pointer;
                 transition: border-color .15s, color .15s; margin-bottom: 24px; }
    #drop-zone.dragover { border-color: #58a6ff; color: #79c0ff; }
    #drop-zone:hover { border-color: #484f58; color: #8b949e; }
    #drop-zone p { font-size: .8rem; margin-top: 6px; }
    #file-input { display: none; }

    /* Upload progress bar */
    #upload-status { display: none; margin-bottom: 16px; padding: 10px 14px;
                     border-radius: 6px; font-size: .78rem; line-height: 1.5; }
    #upload-status.running { display: block; color: #79c0ff; background: #0d1f33;
                              border: 1px solid #79c0ff40; }
    #upload-status.done    { display: block; color: #3fb950; background: #0d2818;
                              border: 1px solid #2ea04340; }
    #upload-status.error   { display: block; color: #f85149; background: #2d0f0f;
                              border: 1px solid #f8514940; }

    /* File table */
    table { width: 100%; border-collapse: collapse; font-size: .78rem; }
    thead th { text-align: left; padding: 8px 10px; color: #484f58;
               text-transform: uppercase; font-size: .65rem; letter-spacing: .07em;
               border-bottom: 1px solid #21262d; user-select: none; }
    thead th[data-col] { cursor: pointer; }
    thead th[data-col]:hover { color: #8b949e; }
    thead th.sort-asc::after  { content: " ▲"; color: #58a6ff; }
    thead th.sort-desc::after { content: " ▼"; color: #58a6ff; }
    tbody tr { border-bottom: 1px solid #161b22; }
    tbody tr:hover { background: #161b22; }
    td { padding: 8px 10px; vertical-align: middle; }
    td.name { color: #79c0ff; max-width: 280px; overflow: hidden;
              text-overflow: ellipsis; white-space: nowrap; }
    td.size, td.modified, td.chunks { color: #8b949e; }
    td.meta { color: #8b949e; font-size: .72rem; max-width: 180px;
              overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .badge { display: inline-block; font-size: .65rem; padding: 1px 6px;
             border-radius: 10px; margin-right: 4px; }
    .badge-pdf { background: #1f2d45; color: #58a6ff; }
    .badge-md  { background: #1a2a1a; color: #3fb950; }
    td.actions { white-space: nowrap; }
    td.actions button { background: none; border: none; cursor: pointer;
                        padding: 3px 7px; border-radius: 4px; font-size: .75rem;
                        color: #484f58; transition: color .15s, background .15s; }
    td.actions button:hover { color: #c9d1d9; background: #21262d; }
    td.actions button.del:hover { color: #f85149; }
    .empty { color: #484f58; font-size: .82rem; padding: 32px 0; text-align: center; }

    /* Modal */
    #modal-overlay { display: none; position: fixed; inset: 0;
                     background: rgba(0,0,0,.7); z-index: 100;
                     align-items: center; justify-content: center; }
    #modal-overlay.open { display: flex; }
    #modal { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
             padding: 24px; width: 480px; max-width: 95vw; }
    #modal h2 { font-size: .85rem; color: #c9d1d9; margin-bottom: 18px;
                padding-bottom: 10px; border-bottom: 1px solid #21262d; }
    .field { margin-bottom: 12px; }
    .field label { display: block; font-size: .65rem; color: #8b949e;
                   text-transform: uppercase; letter-spacing: .07em; margin-bottom: 4px; }
    .field input, .field select { width: 100%; background: #0d1117; border: 1px solid #30363d;
                                   border-radius: 4px; color: #c9d1d9;
                                   font-family: inherit; font-size: .78rem;
                                   padding: 5px 8px; outline: none; }
    .field input:focus, .field select:focus { border-color: #58a6ff; }
    .field select option { background: #0d1117; }
    .modal-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .reingest-row { display: flex; align-items: center; gap: 8px; margin: 14px 0 18px;
                    font-size: .75rem; color: #8b949e; }
    .reingest-row input[type="checkbox"] { width: auto; accent-color: #58a6ff; }
    .modal-actions { display: flex; gap: 8px; justify-content: flex-end; }
    .modal-actions button { padding: 6px 16px; border-radius: 6px; font-family: inherit;
                             font-size: .78rem; font-weight: 600; cursor: pointer;
                             border: 1px solid #30363d; }
    #modal-cancel { background: none; color: #8b949e; }
    #modal-cancel:hover { color: #c9d1d9; }
    #modal-save { background: #238636; border-color: #2ea043; color: #fff; }
    #modal-save:hover { background: #2ea043; }
  </style>
</head>
<body>
  <header>
    <h1>&#9673; Distill — Files</h1>
    <nav>
      <a href="/stats">Stats</a>
    </nav>
  </header>

  <main>
    <div id="drop-zone">
      <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#484f58" stroke-width="1.5">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
        <polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/>
      </svg>
      <p>Drop PDF or Markdown files here, or click to browse</p>
      <input type="file" id="file-input" accept=".pdf,.md" multiple>
    </div>

    <div id="upload-status"></div>

    <table>
      <thead>
        <tr>
          <th data-col="name">Name</th>
          <th data-col="size">Size</th>
          <th data-col="modified">Modified</th>
          <th data-col="chunks">Chunks</th>
          <th data-col="meta">Metadata</th>
          <th></th>
        </tr>
      </thead>
      <tbody id="file-tbody">
        <tr><td colspan="6" class="empty">Loading…</td></tr>
      </tbody>
    </table>
  </main>

  <!-- Sidecar edit modal -->
  <div id="modal-overlay">
    <div id="modal">
      <h2 id="modal-title">Edit Metadata</h2>
      <div class="modal-grid">
        <div class="field">
          <label>Vendor</label>
          <input id="m-vendor" type="text" placeholder="cisco, aruba…">
        </div>
        <div class="field">
          <label>Product</label>
          <input id="m-product" type="text" placeholder="ios-xe, aos-cx…">
        </div>
        <div class="field">
          <label>Version</label>
          <input id="m-version" type="text" placeholder="17.9.1">
        </div>
        <div class="field">
          <label>Doc Type</label>
          <select id="m-doc-type">
            <option value="">— none —</option>
            <option value="cli-reference">cli-reference</option>
            <option value="config-guide">config-guide</option>
            <option value="design-guide">design-guide</option>
            <option value="validated-design">validated-design</option>
            <option value="release-notes">release-notes</option>
            <option value="white-paper">white-paper</option>
          </select>
        </div>
        <div class="field">
          <label>Trust Tier</label>
          <select id="m-tier">
            <option value="1">1 — Vendor documentation</option>
            <option value="2">2 — Validated design</option>
            <option value="3">3 — Internal</option>
          </select>
        </div>
        <div class="field">
          <label>Source Type</label>
          <select id="m-source-type">
            <option value="vendor-doc">vendor-doc</option>
            <option value="validated-design">validated-design</option>
            <option value="internal">internal</option>
          </select>
        </div>
      </div>
      <div class="reingest-row">
        <input type="checkbox" id="m-reingest" checked>
        <label for="m-reingest">Re-ingest file after saving</label>
      </div>
      <div class="modal-actions">
        <button id="modal-cancel">Cancel</button>
        <button id="modal-save">Save</button>
      </div>
    </div>
  </div>

  <script>
    let _currentSidecarPath = null;
    let _pollTimer = null;
    let _allFiles = [];
    let _sortCol = "name";
    let _sortAsc = true;

    function fmtSize(b) {
      if (b >= 1048576) return (b / 1048576).toFixed(1) + " MB";
      if (b >= 1024) return (b / 1024).toFixed(0) + " KB";
      return b + " B";
    }

    function fmtDate(iso) {
      return iso.slice(0, 10);
    }

    function fmtMeta(meta) {
      if (!meta || !Object.keys(meta).length) return "—";
      const parts = [];
      if (meta.vendor) parts.push(meta.vendor);
      if (meta.product) parts.push(meta.product);
      if (meta.version) parts.push(meta.version);
      if (meta.doc_type) parts.push(meta.doc_type);
      return parts.join(" · ") || "—";
    }

    function sortKey(f, col) {
      if (col === "name")     return f.name.toLowerCase();
      if (col === "size")     return f.size;
      if (col === "modified") return f.modified;
      if (col === "chunks")   return f.chunks;
      if (col === "meta")     return fmtMeta(f.meta).toLowerCase();
      return "";
    }

    function renderFiles() {
      const tbody = document.getElementById("file-tbody");
      if (!_allFiles.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty">No documents yet — upload a PDF or Markdown file to get started.</td></tr>';
        return;
      }
      const sorted = [..._allFiles].sort((a, b) => {
        const ka = sortKey(a, _sortCol), kb = sortKey(b, _sortCol);
        const cmp = ka < kb ? -1 : ka > kb ? 1 : 0;
        return _sortAsc ? cmp : -cmp;
      });
      tbody.innerHTML = sorted.map(f => {
        const badge = `<span class="badge badge-${f.type}">.${f.type}</span>`;
        const metaStr = fmtMeta(f.meta);
        const metaTitle = JSON.stringify(f.meta, null, 2);
        return `<tr>
          <td class="name">${badge}${escHtml(f.name)}</td>
          <td class="size">${fmtSize(f.size)}</td>
          <td class="modified">${fmtDate(f.modified)}</td>
          <td class="chunks">${f.chunks.toLocaleString()}</td>
          <td class="meta" title="${escHtml(metaTitle)}">${escHtml(metaStr)}</td>
          <td class="actions">
            <button onclick="downloadFile('${escAttr(f.path)}')" title="Download">&#8595;</button>
            <button onclick="openSidecar('${escAttr(f.path)}')" title="Edit metadata">&#9998;</button>
            <button class="del" onclick="deleteFile('${escAttr(f.path)}', '${escAttr(f.name)}')" title="Delete">&#10005;</button>
          </td>
        </tr>`;
      }).join("");
      document.querySelectorAll("thead th[data-col]").forEach(th => {
        th.classList.toggle("sort-asc",  th.dataset.col === _sortCol && _sortAsc);
        th.classList.toggle("sort-desc", th.dataset.col === _sortCol && !_sortAsc);
      });
    }

    async function loadFiles() {
      const resp = await fetch("/files/list");
      _allFiles = await resp.json();
      renderFiles();
    }

    document.querySelectorAll("thead th[data-col]").forEach(th => {
      th.addEventListener("click", () => {
        if (_sortCol === th.dataset.col) {
          _sortAsc = !_sortAsc;
        } else {
          _sortCol = th.dataset.col;
          _sortAsc = true;
        }
        renderFiles();
      });
    });

    function escHtml(s) {
      return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
    }
    function escAttr(s) {
      return String(s).replace(/\\\\/g,"\\\\\\\\").replace(/'/g,"\\\\'");
    }

    // ── Upload ──

    const dropZone = document.getElementById("drop-zone");
    const fileInput = document.getElementById("file-input");

    dropZone.addEventListener("click", () => fileInput.click());
    dropZone.addEventListener("dragover", e => { e.preventDefault(); dropZone.classList.add("dragover"); });
    dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
    dropZone.addEventListener("drop", e => {
      e.preventDefault();
      dropZone.classList.remove("dragover");
      uploadFiles(e.dataTransfer.files);
    });
    fileInput.addEventListener("change", () => { uploadFiles(fileInput.files); fileInput.value = ""; });

    async function uploadFiles(fileList) {
      for (const file of fileList) {
        await uploadOne(file);
      }
    }

    async function uploadOne(file) {
      const statusEl = document.getElementById("upload-status");
      statusEl.className = "running";
      statusEl.textContent = `Uploading ${file.name}…`;

      const form = new FormData();
      form.append("file", file);

      let jobId;
      try {
        const resp = await fetch("/files/upload", { method: "POST", body: form });
        const data = await resp.json();
        if (!resp.ok) { showUploadError(data.error || "Upload failed"); return; }
        jobId = data.job_id;
      } catch (e) { showUploadError(e.message); return; }

      statusEl.textContent = `Indexing ${file.name}…`;
      _pollTimer = setInterval(async () => {
        try {
          const r = await fetch("/files/status?job_id=" + encodeURIComponent(jobId));
          const job = await r.json();
          if (job.status === "done") {
            clearInterval(_pollTimer);
            statusEl.className = "done";
            statusEl.textContent = `${file.name} — ${job.chunks.toLocaleString()} chunks indexed.`;
            loadFiles();
          } else if (job.status === "error") {
            clearInterval(_pollTimer);
            showUploadError(`Ingest failed: ${job.error}`);
          }
        } catch (_) {}
      }, 1500);
    }

    function showUploadError(msg) {
      const el = document.getElementById("upload-status");
      el.className = "error";
      el.textContent = msg;
    }

    // ── Delete ──

    async function deleteFile(path, name) {
      if (!confirm(`Delete ${name} and remove all its indexed chunks?`)) return;
      const resp = await fetch("/files/delete?path=" + encodeURIComponent(path), { method: "DELETE" });
      if (resp.ok) { loadFiles(); }
      else { const d = await resp.json(); alert(d.error || "Delete failed"); }
    }

    // ── Download ──

    function downloadFile(path) {
      window.location = "/files/download?path=" + encodeURIComponent(path);
    }

    // ── Sidecar modal ──

    async function openSidecar(path) {
      _currentSidecarPath = path;
      document.getElementById("modal-title").textContent =
        "Edit Metadata — " + path.split("/").pop();
      const resp = await fetch("/files/sidecar?path=" + encodeURIComponent(path));
      const meta = await resp.json();
      document.getElementById("m-vendor").value = meta.vendor || "";
      document.getElementById("m-product").value = meta.product || "";
      document.getElementById("m-version").value = meta.version || "";
      document.getElementById("m-doc-type").value = meta.doc_type || "";
      document.getElementById("m-tier").value = String(meta.trust_tier || "1");
      document.getElementById("m-source-type").value = meta.source_type || "vendor-doc";
      document.getElementById("modal-overlay").classList.add("open");
    }

    document.getElementById("modal-cancel").addEventListener("click", () => {
      document.getElementById("modal-overlay").classList.remove("open");
    });
    document.getElementById("modal-overlay").addEventListener("click", e => {
      if (e.target === document.getElementById("modal-overlay"))
        document.getElementById("modal-overlay").classList.remove("open");
    });

    document.getElementById("modal-save").addEventListener("click", async () => {
      const reingest = document.getElementById("m-reingest").checked;
      const body = {
        vendor:      document.getElementById("m-vendor").value.trim().toLowerCase(),
        product:     document.getElementById("m-product").value.trim().toLowerCase(),
        version:     document.getElementById("m-version").value.trim(),
        doc_type:    document.getElementById("m-doc-type").value,
        trust_tier:  parseInt(document.getElementById("m-tier").value),
        source_type: document.getElementById("m-source-type").value,
      };
      const url = "/files/sidecar?path=" + encodeURIComponent(_currentSidecarPath)
                + (reingest ? "&reingest=1" : "");
      const resp = await fetch(url, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      document.getElementById("modal-overlay").classList.remove("open");
      if (reingest && data.job_id) {
        const statusEl = document.getElementById("upload-status");
        const fname = _currentSidecarPath.split("/").pop();
        statusEl.className = "running";
        statusEl.textContent = "Re-indexing " + fname + "…";
        const jobId = data.job_id;
        _pollTimer = setInterval(async () => {
          const r = await fetch("/files/status?job_id=" + encodeURIComponent(jobId));
          const job = await r.json();
          if (job.status === "done") {
            clearInterval(_pollTimer);
            statusEl.className = "done";
            statusEl.textContent = fname + " — " + job.chunks.toLocaleString() + " chunks re-indexed.";
            loadFiles();
          } else if (job.status === "error") {
            clearInterval(_pollTimer);
            showUploadError("Re-ingest failed: " + job.error);
          }
        }, 1500);
      } else {
        loadFiles();
      }
    });

    loadFiles();
  </script>
</body>
</html>"""


# ── Stats page ────────────────────────────────────────────────────────────────

_qdrant_cache: dict = {"data": None, "at": 0.0}


def collect_qdrant_stats() -> dict:
    now = time.monotonic()
    if _qdrant_cache["data"] and (now - _qdrant_cache["at"]) < STATS_TTL:
        return _qdrant_cache["data"]

    try:
        info = qdrant.get_collection(COLLECTION_NAME)
        total_chunks = info.points_count
    except Exception as exc:
        return {"error": str(exc)}

    META_FIELDS = [
        "source",
        "page",
        "vendor",
        "product",
        "version",
        "doc_type",
        "source_type",
        "trust_tier",
    ]
    sources: dict = defaultdict(lambda: {"chunks": 0, "pages": set()})
    offset = None
    while True:
        results, offset = qdrant.scroll(
            collection_name=COLLECTION_NAME,
            limit=500,
            offset=offset,
            with_vectors=False,
            with_payload=META_FIELDS,
        )
        for point in results:
            p = point.payload
            src = p.get("source", "unknown")
            sources[src]["chunks"] += 1
            sources[src]["pages"].add(p.get("page", 0))
            for field in ("vendor", "product", "version", "doc_type", "source_type", "trust_tier"):
                if field not in sources[src] and p.get(field) is not None:
                    sources[src][field] = p[field]
        if offset is None:
            break

    data = {
        "collection": COLLECTION_NAME,
        "total_chunks": total_chunks,
        "total_docs": len(sources),
        "sources": {
            src: {
                "chunks": v["chunks"],
                "pages": len(v["pages"]),
                "vendor": v.get("vendor"),
                "product": v.get("product"),
                "version": v.get("version"),
                "doc_type": v.get("doc_type"),
                "source_type": v.get("source_type"),
                "trust_tier": v.get("trust_tier"),
            }
            for src, v in sorted(sources.items())
        },
        "updated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    _qdrant_cache["data"] = data
    _qdrant_cache["at"] = now
    return data


def score_badge(score: float | None) -> str:
    if score is None:
        color, label = "#484f58", "no results"
    elif score >= 0.70:
        color, label = "#3fb950", f"{score:.3f}"
    elif score >= GAP_THRESHOLD:
        color, label = "#d29922", f"{score:.3f}"
    else:
        color, label = "#f85149", f"{score:.3f}"
    return f'<span style="color:{color};font-weight:600">{label}</span>'


def tag(value: str | None) -> str:
    if not value:
        return '<span style="color:#484f58">—</span>'
    return (
        f'<span style="background:#21262d;border:1px solid #30363d;border-radius:4px;'
        f'padding:.1rem .4rem;font-size:.75rem">{value}</span>'
    )


def tier_badge_html(tier: int | None) -> str:
    if tier is None:
        return '<span style="color:#484f58">—</span>'
    colors = {1: "#3fb950", 2: "#58a6ff", 3: "#d29922", 4: "#f85149"}
    labels = {1: "vendor t1", 2: "validated t2", 3: "internal t3", 4: "community t4"}
    color = colors.get(tier, "#8b949e")
    label = labels.get(tier, f"tier-{tier}")
    return (
        f'<span style="color:{color};background:#21262d;border:1px solid {color}40;'
        f'border-radius:4px;padding:.1rem .4rem;font-size:.75rem">{label}</span>'
    )


def _render_active_banner(active: dict) -> str:
    if not active:
        return ""
    now = time.monotonic()
    rows = "".join(
        f'<div class="active-row">'
        f'<span class="elapsed">{now - v["started_at"]:.1f}s</span>'
        f'<span class="aquery">"{v["query"]}"</span>'
        f' <span style="color:#8b949e;font-size:.75rem">started {v["started_ts"]}</span>'
        f"</div>"
        for v in active.values()
    )
    return (
        f'<div class="active-banner">'
        f'<div class="label"><span class="blink">&#9679;</span> '
        f"{len(active)} query running{'' if len(active) == 1 else 's'} — refreshing every 3s</div>"
        f"{rows}"
        f"</div>"
    )


def render_stats(qdrant_stats: dict, active: dict) -> str:
    if "error" in qdrant_stats:
        return (
            f'<!DOCTYPE html><html><body style="font-family:monospace;background:#111;'
            f'color:#f55;padding:2rem"><h2>Error</h2><pre>{qdrant_stats["error"]}</pre>'
            f"</body></html>"
        )

    refresh_interval = 3 if active else STATS_TTL
    today = datetime.now(UTC).strftime("%Y-%m-%d")

    total_queries = query_db("SELECT COUNT(*) FROM queries")[0][0]
    queries_today = query_db("SELECT COUNT(*) FROM queries WHERE ts >= ?", (today,))[0][0]
    avg_latency = query_db("SELECT ROUND(AVG(latency_ms)) FROM queries")[0][0] or 0
    avg_score = (
        query_db("SELECT ROUND(AVG(top_score),3) FROM queries WHERE top_score IS NOT NULL")[0][0]
        or 0
    )

    recent_rows = query_db(
        "SELECT ts, query, vendor, product, top_score, result_count, top_source, top_page, latency_ms "
        "FROM queries ORDER BY id DESC LIMIT 30"
    )
    gap_rows = query_db(
        "SELECT query, MIN(top_score) as best, COUNT(*) as times "
        "FROM queries WHERE top_score < ? "
        "GROUP BY query ORDER BY times DESC, best ASC LIMIT 20",
        (GAP_THRESHOLD,),
    )
    top_sources = query_db(
        "SELECT top_source, COUNT(*) as refs, ROUND(AVG(top_score),3) "
        "FROM queries WHERE top_source IS NOT NULL "
        "GROUP BY top_source ORDER BY refs DESC LIMIT 10"
    )
    slow_queries = query_db(
        "SELECT ts, query, latency_ms, top_score, top_source "
        "FROM queries ORDER BY latency_ms DESC LIMIT 10"
    )

    def catalog_rows():
        if not qdrant_stats["sources"]:
            return '<tr><td colspan="8" class="empty">No documents ingested yet</td></tr>'
        return "".join(
            f"<tr>"
            f'<td class="src">{src}</td>'
            f"<td>{tag(info.get('vendor'))}</td>"
            f"<td>{tag(info.get('product'))}</td>"
            f"<td>{tag(info.get('version'))}</td>"
            f"<td>{tag(info.get('doc_type'))}</td>"
            f"<td>{tier_badge_html(info.get('trust_tier'))}</td>"
            f'<td class="num">{info["pages"]}</td>'
            f'<td class="num">{info["chunks"]:,}</td>'
            f"</tr>"
            for src, info in qdrant_stats["sources"].items()
        )

    def recent_query_rows():
        if not recent_rows:
            return '<tr><td colspan="7" class="empty">No queries yet</td></tr>'
        out = []
        for ts, q, v, p, score, rc, src, pg, lat in recent_rows:
            src_cell = f"{src} p.{pg}" if src else "—"
            filters = "  ".join(x for x in [v, p] if x) or "—"
            out.append(
                f"<tr>"
                f'<td class="ts">{ts}</td>'
                f'<td class="qtext">{q}</td>'
                f'<td style="font-size:.75rem;color:#8b949e">{filters}</td>'
                f'<td class="num">{score_badge(score)}</td>'
                f'<td class="num">{rc if rc is not None else 0}</td>'
                f'<td class="src">{src_cell}</td>'
                f'<td class="num">{lat} ms</td>'
                f"</tr>"
            )
        return "".join(out)

    def gap_rows_html():
        if not gap_rows:
            return f'<tr><td colspan="3" class="empty">No gaps detected (all scores ≥ {GAP_THRESHOLD})</td></tr>'
        return "".join(
            f'<tr><td class="qtext">{q}</td>'
            f'<td class="num">{score_badge(best)}</td>'
            f'<td class="num">{n}</td></tr>'
            for q, best, n in gap_rows
        )

    def top_source_rows():
        if not top_sources:
            return '<tr><td colspan="3" class="empty">No queries yet</td></tr>'
        return "".join(
            f'<tr><td class="src">{src}</td>'
            f'<td class="num">{refs}</td>'
            f'<td class="num">{score_badge(avg)}</td></tr>'
            for src, refs, avg in top_sources
        )

    def slow_query_rows():
        if not slow_queries:
            return '<tr><td colspan="5" class="empty">No queries yet</td></tr>'
        return "".join(
            f'<tr><td class="ts">{ts}</td>'
            f'<td class="qtext">{q}</td>'
            f'<td class="num" style="color:#f85149">{lat} ms</td>'
            f'<td class="num">{score_badge(score)}</td>'
            f'<td class="src">{src or "—"}</td></tr>'
            for ts, q, lat, score, src in slow_queries
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="{refresh_interval}">
  <title>Distill — Stats</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: ui-monospace, "Cascadia Code", "Fira Mono", monospace;
      background: #0d1117; color: #c9d1d9; padding: 2rem; min-height: 100vh;
    }}
    h1 {{ font-size: 1.4rem; color: #58a6ff; margin-bottom: 1.5rem; letter-spacing: .03em; }}
    h2 {{ font-size: .85rem; color: #8b949e; text-transform: uppercase;
          letter-spacing: .1em; margin: 2rem 0 .75rem; }}
    .cards {{ display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: .5rem; }}
    .card {{
      background: #161b22; border: 1px solid #30363d;
      border-radius: 8px; padding: 1rem 1.5rem; min-width: 140px;
    }}
    .card .label {{ font-size: .65rem; color: #8b949e; text-transform: uppercase; letter-spacing: .08em; }}
    .card .value {{ font-size: 1.8rem; font-weight: 700; color: #58a6ff; margin-top: .2rem; }}
    .card .value.sm {{ font-size: 1rem; padding-top: .3rem; }}
    table {{
      width: 100%; border-collapse: collapse;
      background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden;
    }}
    th {{
      background: #21262d; color: #8b949e;
      font-size: .65rem; text-transform: uppercase; letter-spacing: .08em;
      padding: .55rem 1rem; text-align: left; white-space: nowrap;
    }}
    td {{ padding: .5rem 1rem; border-top: 1px solid #21262d; font-size: .82rem; vertical-align: middle; }}
    td.num   {{ text-align: right; white-space: nowrap; }}
    td.ts    {{ color: #8b949e; white-space: nowrap; font-size: .75rem; }}
    td.src   {{ word-break: break-all; font-size: .78rem; color: #79c0ff; }}
    td.qtext {{ max-width: 380px; word-break: break-word; }}
    td.empty {{ text-align: center; color: #484f58; padding: 1.5rem; }}
    tr:hover td {{ background: #1c2128; }}
    th[style*="right"] {{ text-align: right; }}
    .footer  {{ margin-top: 1.5rem; font-size: .7rem; color: #484f58; }}
    .dot     {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%;
                background: #3fb950; margin-right: .4rem; }}
    .gap-note {{ font-size: .72rem; color: #8b949e; margin-bottom: .5rem; }}
    .active-banner {{
      background: #1a2d1a; border: 1px solid #3fb950;
      border-radius: 8px; padding: .75rem 1rem; margin-bottom: 1.5rem;
    }}
    .active-banner .label {{
      font-size: .7rem; color: #3fb950; text-transform: uppercase;
      letter-spacing: .08em; margin-bottom: .5rem;
    }}
    .active-row {{ font-size: .85rem; padding: .2rem 0; }}
    .active-row .elapsed {{ color: #3fb950; font-weight: 600; margin-right: .75rem; }}
    @keyframes pulse {{ 0%,100% {{ opacity:1 }} 50% {{ opacity:.4 }} }}
    .blink {{ display:inline-block; animation: pulse 1.2s ease-in-out infinite; }}
  </style>
</head>
<body>
  <h1>&#9673; Network Docs RAG#9673; Distill</h1>

  {_render_active_banner(active)}

  <div class="cards">
    <div class="card"><div class="label">Collection</div><div class="value sm">{qdrant_stats["collection"]}</div></div>
    <div class="card"><div class="label">Documents</div><div class="value">{qdrant_stats["total_docs"]}</div></div>
    <div class="card"><div class="label">Total Chunks</div><div class="value">{qdrant_stats["total_chunks"]:,}</div></div>
    <div class="card"><div class="label">Queries Today</div><div class="value">{queries_today:,}</div></div>
    <div class="card"><div class="label">Total Queries</div><div class="value">{total_queries:,}</div></div>
    <div class="card"><div class="label">Avg Latency</div><div class="value">{avg_latency}<span style="font-size:.9rem;color:#8b949e"> ms</span></div></div>
    <div class="card"><div class="label">Avg Score</div><div class="value">{avg_score}</div></div>
  </div>

  <h2>Document Catalog</h2>
  <table>
    <thead><tr>
      <th>Document</th><th>Vendor</th><th>Product</th>
      <th>Version</th><th>Doc Type</th><th>Trust Tier</th>
      <th style="text-align:right">Pages</th><th style="text-align:right">Chunks</th>
    </tr></thead>
    <tbody>{catalog_rows()}</tbody>
  </table>

  <h2>Recent Queries</h2>
  <table>
    <thead><tr>
      <th>Time (UTC)</th><th>Query</th><th>Filters</th>
      <th style="text-align:right">Score</th><th style="text-align:right">Results</th>
      <th>Top Source</th><th style="text-align:right">Latency</th>
    </tr></thead>
    <tbody>{recent_query_rows()}</tbody>
  </table>

  <h2>Coverage Gaps</h2>
  <p class="gap-note">Queries scoring below {GAP_THRESHOLD} — topics likely missing from your ingested docs.</p>
  <table>
    <thead><tr>
      <th>Query</th>
      <th style="text-align:right">Best Score</th>
      <th style="text-align:right">Times Asked</th>
    </tr></thead>
    <tbody>{gap_rows_html()}</tbody>
  </table>

  <h2>Most Referenced Sources</h2>
  <table>
    <thead><tr>
      <th>Document</th>
      <th style="text-align:right">Times Referenced</th>
      <th style="text-align:right">Avg Score</th>
    </tr></thead>
    <tbody>{top_source_rows()}</tbody>
  </table>

  <h2>Slowest Queries</h2>
  <table>
    <thead><tr>
      <th>Time (UTC)</th><th>Query</th>
      <th style="text-align:right">Latency</th>
      <th style="text-align:right">Score</th><th>Top Source</th>
    </tr></thead>
    <tbody>{slow_query_rows()}</tbody>
  </table>

  <p class="footer">
    <span class="dot"></span>Auto-refreshes every {STATS_TTL}s
    &nbsp;·&nbsp; Doc index cached {qdrant_stats["updated_at"]}
    &nbsp;·&nbsp; Score: <span style="color:#3fb950">≥0.70 good</span>
    <span style="color:#d29922"> ≥0.50 ok</span>
    <span style="color:#f85149"> &lt;0.50 gap</span>
    &nbsp;·&nbsp; Tiers:
    <span style="color:#3fb950">1=vendor</span>
    <span style="color:#58a6ff"> 2=validated</span>
    <span style="color:#d29922"> 3=internal</span>
    <span style="color:#f85149"> 4=community (search_community only)</span>
  </p>
</body>
</html>"""


@mcp.custom_route("/stats", methods=["GET"])
async def stats_handler(request):
    with _active_lock:
        active_snapshot = dict(_active)
    return HTMLResponse(render_stats(collect_qdrant_stats(), active_snapshot))


# ── App assembly ──────────────────────────────────────────────────────────────


async def main():
    init_db()
    init_reranker()

    # sse_starlette.EventSourceResponse supports a `ping` parameter that sends
    # SSE comment lines (": ping") to reset router/firewall idle-TCP timers.
    # mcp 1.27 constructs EventSourceResponse without ping, so we inject the
    # default here before sse_app() builds the transport.
    import functools

    import mcp.server.sse as _mcp_sse
    import sse_starlette

    _mcp_sse.EventSourceResponse = functools.partial(sse_starlette.EventSourceResponse, ping=30)

    app = mcp.sse_app()

    config = uvicorn.Config(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main())
