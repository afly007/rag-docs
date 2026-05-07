# Network Docs RAG

A self-hosted RAG pipeline that ingests vendor PDF documentation and exposes a `search_docs` tool via MCP server, allowing Claude to retrieve relevant CLI syntax, configuration examples, and design references mid-conversation.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 Remote Server                   │
│                                                 │
│  ┌──────────┐      ┌──────────────────────────┐ │
│  │  Qdrant  │◄─────│  MCP Server  :8000       │ │
│  │  :6333   │      │  /sse  — search_docs()   │ │
│  └──────────┘      │  /stats — dashboard      │ │
│        ▲           └──────────────────────────┘ │
│  ┌─────┴──────────────────────────┐             │
│  │  Ingest (one-shot container)   │             │
│  │  ./docs/*.pdf → chunks →       │             │
│  │  embeddings → Qdrant           │             │
│  └────────────────────────────────┘             │
└─────────────────────────────────────────────────┘
                    ▲
          SSH tunnel / Tailscale / VPN
                    │
       ┌────────────┴───────────────┐
       │  Claude Code  ~/.claude/   │
       │  Claude Desktop  mcp-remote│
       └────────────────────────────┘
```

**Components:**
| Service | Image / Build | Purpose |
|---|---|---|
| `qdrant` | `qdrant/qdrant:latest` | Vector database — stores embeddings and full text |
| `mcp-server` | `./mcp-server` | FastMCP over SSE — exposes `search_docs` tool, stats dashboard, query log |
| `ingest` | `./ingest` | One-shot PDF ingestion (run manually, profile-gated) |

**Embeddings:** OpenAI `text-embedding-3-small` (1536 dims, cosine similarity)  
**Chunking:** ~750 tokens per chunk, 100-token overlap, per page  
**Persistent volumes:** `qdrant_data` (vectors), `mcp_data` (query log SQLite DB)

---

## Prerequisites

- Docker + Docker Compose v2
- OpenAI API key
- Claude Code CLI and/or Claude desktop app (local machine)

---

## Setup

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:
```
OPENAI_API_KEY=sk-...
COLLECTION_NAME=network_docs   # optional — change to namespace multiple doc sets
```

### 2. Start Qdrant and MCP server

```bash
docker compose up -d
```

Both `qdrant_data` and `mcp_data` are Docker volumes — they survive container restarts and rebuilds.

### 3. Add PDFs and ingest

Copy vendor PDFs into `./docs/`, then run the ingest container:

```bash
# ingest everything in ./docs/
docker compose --profile ingest run --rm ingest

# or target specific files
docker compose --profile ingest run --rm ingest /docs/cisco-ios-xe-17.pdf
```

Progress is shown per file:

```
Found 2 PDF(s) to ingest

────────────────────────────────────────────────────────────
File:  cisco-ios-xe-17.pdf  (42.3 MB)
Pages: 1847 total, 1831 with text
Chunks: 4209  (43 embedding batches)
  Embedding: 100%|████████████| 43/43 [00:18<00:00,  2.3batch/s]
  Storing:   100%|████████████| 22/22 [00:04<00:00]
Done:  4209 chunks stored in 23.1s

════════════════════════════════════════════════════════════
Finished: 2 file(s), 7431 chunks total in 41.6s
```

Ingestion is **idempotent** — re-running on the same file upserts identical vectors. Safe to re-run after adding new PDFs.

### Tagging documents with metadata

For each PDF, create a sidecar `.json` file with the same base name in the same directory:

```
docs/
  cisco-ios-xe-17.pdf
  cisco-ios-xe-17.json      ← sidecar
  juniper-junos-23.pdf
  juniper-junos-23.json
```

Sidecar format (all fields optional):
```json
{
  "vendor":   "cisco",
  "product":  "ios-xe",
  "version":  "17.9.1",
  "doc_type": "cli-reference"
}
```

Common `doc_type` values: `cli-reference`, `config-guide`, `design-guide`, `release-notes`, `white-paper`

Without a sidecar the document is still ingested and searchable — metadata just won't be available for filtering. You can add sidecars later and re-ingest with `--force` to backfill.

---

## Connecting Claude

### Claude Code (CLI)

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "network-docs": {
      "type": "sse",
      "url": "http://YOUR_SERVER_IP:8000/sse"
    }
  }
}
```

### Claude Desktop app

The desktop app requires `mcp-remote` as a stdio-to-SSE bridge. Install Node first if needed (`brew install node`), then add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "network-docs": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://YOUR_SERVER_IP:8000/sse", "--allow-http"]
    }
  }
}
```

The `--allow-http` flag is required for non-localhost URLs. Restart the desktop app after saving.

### SSH tunnel (if not exposing port 8000 publicly)

```bash
ssh -L 8000:localhost:8000 user@your-server
```

Then use `http://localhost:8000/sse` in either config above (and omit `--allow-http` for the desktop app).

---

## Usage

Two MCP tools are available:

### `list_docs()`

Returns the full document catalog — vendors, products, versions, doc types, and chunk counts. Claude should call this first when it needs to know what's available or what filter values to use.

```
You: What documentation do you have access to?

Claude: [calls list_docs()]
        → Collection: network_docs  Documents: 12  Chunks: 47,832

        Available vendors:   arista, cisco, juniper
        Available products:  eos, ios-xe, ios-xr, junos
        Available doc_types: cli-reference, config-guide

        Document                                      Vendor    Product   Version  Doc Type        Chunks
        ──────────────────────────────────────────────────────────────────────────────────
        cisco-ios-xe-17-cli.pdf                       cisco     ios-xe    17.9.1   cli-reference   4,209
        juniper-junos-23-config.pdf                   juniper   junos     23.2R1   config-guide    3,102
        ...
```

### `search_docs(query, vendor, product, doc_type)`

Searches the vector store and returns the top 5 matching chunks. All filter arguments are optional.

```
You: How do I configure a BGP route reflector on IOS-XE?

Claude: [calls search_docs("BGP route reflector configuration", vendor="cisco", product="ios-xe")]
        → returns top 5 chunks from Cisco IOS-XE docs only
        → answers using exact CLI syntax from the vendor docs
```

Without filters, all documents are searched:
```
search_docs("OSPF area types comparison")
search_docs("QoS DSCP marking policy-map", vendor="cisco")
search_docs("EVPN type-5 route", product="junos")
search_docs("BGP community list", doc_type="cli-reference")
```

**Note:** Filtering only works on documents that were ingested with a sidecar `.json` file. Documents without metadata are always included in unfiltered searches.

---

## Stats dashboard

Open `http://YOUR_SERVER_IP:8000/stats` in a browser. Auto-refreshes every 60 seconds.

**Cards:** Documents · Total Chunks · Queries Today · Total Queries · Avg Latency · Avg Score

**Sections:**

| Section | What it shows |
|---|---|
| Ingested Documents | All ingested PDFs with page and chunk counts |
| Recent Queries | Last 30 queries — time, text, score, source, latency |
| Coverage Gaps | Queries scoring below 0.50 — grouped by query, sorted by frequency. These are topics missing from your corpus. |
| Most Referenced Sources | Which documents get retrieved most, with average relevance score |
| Slowest Queries | Top 10 by latency — useful for spotting embed API bottlenecks |

**Score legend:** green ≥ 0.70 (good match) · yellow ≥ 0.50 (ok) · red < 0.50 (gap)

Every query is persisted to `/data/queries.db` (SQLite, WAL mode) inside the `mcp_data` Docker volume. The DB survives container restarts.

---

## Managing multiple document sets

Set `COLLECTION_NAME` to separate vendor docs into named collections:

```bash
# Cisco docs
COLLECTION_NAME=cisco docker compose --profile ingest run --rm ingest

# Juniper docs
COLLECTION_NAME=juniper docker compose --profile ingest run --rm ingest
```

The MCP server searches whichever collection it was started with. To switch collections, update `.env` and restart:

```bash
docker compose up -d mcp-server
```

---

## Operations

### View live logs
```bash
docker compose logs -f mcp-server
docker compose logs -f qdrant
```

### Query the log database directly
```bash
docker run --rm -v rag-docs_mcp_data:/data alpine \
  sh -c "apk add -q sqlite && sqlite3 /data/queries.db \
  'SELECT ts, query, top_score, top_source FROM queries ORDER BY id DESC LIMIT 20'"
```

### Delete and re-ingest a collection
```bash
curl -X DELETE http://localhost:6333/collections/network_docs
docker compose --profile ingest run --rm ingest
```

### Rebuild after code changes
```bash
docker compose build mcp-server
docker compose up -d mcp-server
```

### Check Qdrant collection info
```bash
curl http://localhost:6333/collections/network_docs
```

---

## Switching to local embeddings

To eliminate OpenAI API costs, swap `text-embedding-3-small` for a local model (e.g. `nomic-embed-text` via Ollama). The embedding dimension changes from 1536 to 768, so the existing Qdrant collection must be deleted and re-created before re-ingesting. Update `EMBEDDING_MODEL` and `EMBEDDING_DIM` in both `ingest/ingest.py` and `mcp-server/server.py`.
