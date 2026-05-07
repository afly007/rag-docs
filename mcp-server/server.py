import asyncio
import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime

import tiktoken
import uvicorn
from mcp.server.fastmcp import FastMCP
from openai import AsyncOpenAI
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import FieldCondition, Filter, Fusion, MatchValue, Prefetch, SparseVector
from starlette.responses import HTMLResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", 6333))
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "network_docs")
DB_PATH = os.environ.get("DB_PATH", "/data/queries.db")
EMBEDDING_MODEL = "text-embedding-3-small"
TOP_K = 5
PREFETCH_K = 20  # candidates per retriever fed into RRF fusion
STATS_TTL = 60
GAP_THRESHOLD = 0.02  # RRF scores are much smaller than cosine scores
SEARCH_TIMEOUT = 25

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
enc = tiktoken.get_encoding("cl100k_base")


def compute_sparse(text: str) -> SparseVector:
    counts: dict[int, float] = {}
    for tid in enc.encode(text):
        counts[tid] = counts.get(tid, 0.0) + 1.0
    return SparseVector(indices=list(counts), values=list(counts.values()))


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


def build_filter(vendor: str, product: str, doc_type: str, version: str) -> Filter | None:
    conditions = []
    if vendor:
        conditions.append(FieldCondition(key="vendor", match=MatchValue(value=vendor)))
    if product:
        conditions.append(FieldCondition(key="product", match=MatchValue(value=product)))
    if doc_type:
        conditions.append(FieldCondition(key="doc_type", match=MatchValue(value=doc_type)))
    if version:
        conditions.append(FieldCondition(key="version", match=MatchValue(value=version)))
    return Filter(must=conditions) if conditions else None


qdrant = connect_qdrant()
mcp = FastMCP("network-docs", host="0.0.0.0", port=8000)


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
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT    NOT NULL,
                query        TEXT    NOT NULL,
                vendor       TEXT,
                product      TEXT,
                doc_type     TEXT,
                top_score    REAL,
                result_count INTEGER,
                top_source   TEXT,
                top_page     INTEGER,
                latency_ms   INTEGER
            )
        """)
        # Migrate: add columns absent from older schema versions
        existing = {row[1] for row in conn.execute("PRAGMA table_info(queries)")}
        for col in ("vendor", "product", "doc_type"):
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
):
    ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with _db_lock, sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT INTO queries "
                "(ts, query, vendor, product, doc_type, top_score, result_count, top_source, top_page, latency_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ),
            )
            conn.commit()
    except Exception as exc:
        log.warning("Failed to log query: %s", exc)


def query_db(sql: str, params: tuple = ()) -> list[tuple]:
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        return conn.execute(sql, params).fetchall()


# ── MCP tools ─────────────────────────────────────────────────────────────────


@mcp.tool()
async def list_docs() -> str:
    """
    List all ingested documents with their metadata (vendor, product, version, doc_type)
    and the filter values accepted by search_docs(). Call this first to discover what
    is available before filtering a search.
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

    lines = [
        f"Collection: {stats['collection']}",
        f"Documents:  {stats['total_docs']}   Chunks: {stats['total_chunks']:,}",
        "",
        f"Available vendors:   {', '.join(vendors) or '(none tagged)'}",
        f"Available products:  {', '.join(products) or '(none tagged)'}",
        f"Available versions:  {', '.join(versions) or '(none tagged)'}",
        f"Available doc_types: {', '.join(doc_types) or '(none tagged)'}",
        "",
        f"{'Document':<45} {'Vendor':<12} {'Product':<12} {'Version':<10} {'Doc Type':<16} Chunks",
        "─" * 110,
    ]
    for src, info in sorted(stats["sources"].items()):
        lines.append(
            f"{src:<45} "
            f"{info.get('vendor') or '—':<12} "
            f"{info.get('product') or '—':<12} "
            f"{info.get('version') or '—':<10} "
            f"{info.get('doc_type') or '—':<16} "
            f"{info['chunks']:,}"
        )

    lines += [
        "",
        "Use search_docs(query, vendor=..., product=..., doc_type=..., version=...) to filter results.",
        "Untagged documents are searched when no filter is specified.",
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
) -> str:
    """
    Search ingested vendor documentation for the most relevant sections.

    Returns the top 5 matching chunks with source, page, relevance score, and text.
    Use list_docs() first to see available filter values.

    Args:
        query:    What to search for.
        vendor:   Optional — filter to a specific vendor (e.g. "cisco", "juniper").
        product:  Optional — filter to a specific product (e.g. "ios-xe", "junos").
        doc_type: Optional — filter by document type (e.g. "cli-reference", "config-guide").
        version:  Optional — filter to a specific version (e.g. "10.16", "17.9.1").
    """
    filter_desc = "  ".join(
        f"{k}={v}"
        for k, v in [
            ("vendor", vendor),
            ("product", product),
            ("doc_type", doc_type),
            ("version", version),
        ]
        if v
    )
    log.info("search_docs query=%r  filters: %s", query, filter_desc or "none")

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
            resp = await openai_client.embeddings.create(model=EMBEDDING_MODEL, input=query)
            query_vector = resp.data[0].embedding

            try:
                result = qdrant.query_points(
                    collection_name=COLLECTION_NAME,
                    prefetch=[
                        Prefetch(query=query_vector, using="dense", limit=PREFETCH_K),
                        Prefetch(query=compute_sparse(query), using="bm25", limit=PREFETCH_K),
                    ],
                    query=Fusion.RRF,
                    query_filter=build_filter(vendor, product, doc_type, version),
                    limit=TOP_K,
                    with_payload=True,
                )
                hits = result.points
            except UnexpectedResponse as exc:
                if "doesn't exist" in str(exc):
                    return "No documents have been ingested yet."
                raise

        latency_ms = int((time.monotonic() - t0) * 1000)

        if not hits:
            no_result_msg = "No relevant documentation found."
            if vendor or product or doc_type:
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
    for i, hit in enumerate(hits, 1):
        p = hit.payload
        meta_parts = "  ".join(
            f"{k}={p[k]}" for k in ("vendor", "product", "version", "doc_type") if p.get(k)
        )
        header = f"[{i}] {p['source']}  |  page {p['page']}  |  score {hit.score:.3f}"
        if meta_parts:
            header += f"  |  {meta_parts}"

        chunk_idx = p.get("chunk_index")
        if chunk_idx is not None:
            prev_text, next_text = _fetch_adjacent_text(p["source"], chunk_idx)
        else:
            prev_text = next_text = None

        body = _build_context_block(p["text"], prev_text, next_text)
        sections.append(f"{header}\n{body}")

    return "\n\n---\n\n".join(sections)


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

    META_FIELDS = ["source", "page", "vendor", "product", "version", "doc_type"]
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
            for field in ("vendor", "product", "version", "doc_type"):
                if field not in sources[src] and p.get(field):
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
    return f'<span style="background:#21262d;border:1px solid #30363d;border-radius:4px;padding:.1rem .4rem;font-size:.75rem">{value}</span>'


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
            return '<tr><td colspan="7" class="empty">No documents ingested yet</td></tr>'
        return "".join(
            f"<tr>"
            f'<td class="src">{src}</td>'
            f"<td>{tag(info.get('vendor'))}</td>"
            f"<td>{tag(info.get('product'))}</td>"
            f"<td>{tag(info.get('version'))}</td>"
            f"<td>{tag(info.get('doc_type'))}</td>"
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
  <title>Network Docs RAG — Stats</title>
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
  <h1>&#9673; Network Docs RAG</h1>

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
      <th>Version</th><th>Doc Type</th>
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
