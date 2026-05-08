# Network Docs RAG

A self-hosted RAG pipeline that ingests vendor PDF documentation and exposes `search_docs` and `list_docs` tools via MCP server, allowing Claude to retrieve relevant CLI syntax, configuration examples, and design references mid-conversation.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 Remote Server                   │
│                                                 │
│  ┌──────────┐      ┌──────────────────────────┐ │
│  │  Qdrant  │◄─────│  MCP Server  :8000       │ │
│  │  :6333   │      │  /sse  — search_docs()   │ │
│  └──────────┘      │         list_docs()      │ │
│        ▲           │  /stats — dashboard      │ │
│  ┌─────┴──────────────────────────┐            │ │
│  │  Ingest (one-shot container)   │            │ │
│  │  ./docs/*.pdf → chunks →       │            │ │
│  │  dense + BM25 vectors → Qdrant │            │ │
│  └────────────────────────────────┘            │ │
│                    └──────────────────────────┘ │
└─────────────────────────────────────────────────┘
                    ▲
          SSH tunnel / local network
                    │
       ┌────────────┴───────────────┐
       │  Claude Code  ~/.claude/   │
       │  Claude Desktop  mcp-remote│
       └────────────────────────────┘
```

**Components:**
| Service | Image / Build | Purpose |
|---|---|---|
| `qdrant` | `qdrant/qdrant:latest` | Vector database — stores dense + sparse vectors and full text |
| `mcp-server` | `./mcp-server` | FastMCP over SSE — exposes `search_docs` and `list_docs` tools, stats dashboard, query log |
| `ingest` | `./ingest` | One-shot PDF ingestion (run manually, profile-gated) |
| `ingest-watch` | `./ingest` | Continuous watch mode — polls `./docs/` every 30s and ingests new PDFs automatically (profile-gated) |

**Embeddings:** OpenAI `text-embedding-3-small` (1536 dims, cosine similarity)
**Search:** Hybrid — dense vector + BM25 sparse (tiktoken TF · Qdrant IDF), fused with Reciprocal Rank Fusion, optional cross-encoder re-ranking
**Chunking:** Section-aware — splits at PDF TOC boundaries so CLI blocks and tables stay intact; falls back to fixed 750-token stride for docs with no TOC
**Persistent volumes:** `qdrant_data` (vectors), `mcp_data` (query log SQLite DB)

---

## Prerequisites

- Docker + Docker Compose v2
- OpenAI API key
- Claude Code CLI and/or Claude Desktop app (local machine)

---

## Setup

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:
```
OPENAI_API_KEY=sk-...
COLLECTION_NAME=network_docs        # optional — change to namespace multiple doc sets
IMAGE_BASE=ghcr.io/afly007/rag-docs # required for docker compose pull to resolve GHCR images
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
make ingest

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

#### Option A — Auto-generate sidecars (recommended)

```bash
make gen-sidecars
```

This calls gpt-4o-mini on the first 10 pages of each PDF to extract vendor, product, version, and doc_type, then writes a draft `.json` sidecar alongside each PDF. Review and edit the files before ingesting.

#### Option B — Write sidecars manually

For each PDF, create a sidecar `.json` with the same base name:

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

Without a sidecar the document is still ingested and searchable — metadata just won't be available for filtering. You can add sidecars later and re-ingest with `make ingest-force` to backfill.

### Enabling re-ranking (optional)

Re-ranking improves precision by running a cross-encoder over the top 20 retrieved chunks before returning the top 5. Set `RERANKER` in `.env`:

```bash
# Local cross-encoder — no API costs, ~22 MB model downloaded on first start
RERANKER=local

# Cohere Rerank API — requires API key, negligible latency
RERANKER=cohere
COHERE_API_KEY=...
```

Then restart: `docker compose up -d mcp-server`

The local model (`ms-marco-MiniLM-L-12-v2`) is cached in the `mcp_data` volume and only downloaded once. Startup log will show `Local re-ranker ready` when active.

### Auto-ingest watch (optional)

Instead of running `make ingest` manually after every PDF drop, start the watch container:

```bash
make watch
```

This starts `ingest-watch` in the background. It polls `./docs/` every 30 seconds and ingests any PDF not yet in the collection. Drop a file and it will be searchable within 30 seconds.

```bash
docker compose logs -f ingest-watch   # tail ingestion log
make watch-stop                        # stop it
```

The watch container uses `restart: unless-stopped` so it survives server reboots as long as `make up` has been run once.

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

Returns the full document catalog — vendors, products, versions, doc types, and chunk counts. Call this first to discover what filter values are available.

```
You: What documentation do you have access to?

Claude: [calls list_docs()]
        → Collection: network_docs  Documents: 12  Chunks: 47,832

        Available vendors:   arista, cisco, juniper
        Available products:  eos, ios-xe, ios-xr, junos
        Available versions:  17.9.1, 23.2R1
        Available doc_types: cli-reference, config-guide

        Document                                      Vendor    Product   Version  Doc Type        Chunks
        ──────────────────────────────────────────────────────────────────────────────────
        cisco-ios-xe-17-cli.pdf                       cisco     ios-xe    17.9.1   cli-reference   4,209
        juniper-junos-23-config.pdf                   juniper   junos     23.2R1   config-guide    3,102
        ...
```

### `search_docs(query, vendor, product, doc_type, version)`

Searches using hybrid retrieval (BM25 + dense vectors, RRF fused) and returns the top 5 matching chunks with surrounding context. All filter arguments are optional.

```
You: Search my network docs for how to configure LACP on AOS-CX

Claude: [calls search_docs("LACP link aggregation configuration", product="aos-cx")]
        → returns top 5 chunks from AOS-CX docs
        → answers using exact CLI syntax from the vendor docs
```

Filter examples:
```
search_docs("OSPF area types comparison")
search_docs("QoS DSCP marking policy-map", vendor="cisco")
search_docs("EVPN type-5 route", product="junos")
search_docs("BGP community list", doc_type="cli-reference")
search_docs("spanning-tree port-priority", version="10.16")
```

**Re-ranking:** When `RERANKER=local` or `RERANKER=cohere` is set, `search_docs` fetches 20 candidates from Qdrant and a cross-encoder re-scores each (query, chunk) pair together before returning the top 5. More accurate than vector similarity alone for ambiguous queries.

**Prompting tip:** Claude won't use MCP tools unless the question makes it obvious. Phrases like "search my network docs for…", "according to the AOS-CX documentation…", or "use the network-docs tool to look up…" reliably trigger tool calls.

**Note:** Filtering only works on documents ingested with a sidecar `.json` file. Documents without metadata are always included in unfiltered searches.

---

## Stats dashboard

Open `http://YOUR_SERVER_IP:8000/stats` in a browser. Auto-refreshes every 60 seconds.

**Cards:** Documents · Total Chunks · Queries Today · Total Queries · Avg Latency · Avg Score

**Sections:**

| Section | What it shows |
|---|---|
| Document Catalog | All ingested PDFs with vendor, product, version, page and chunk counts |
| Recent Queries | Last 30 queries — time, text, filters, score, source, latency |
| Coverage Gaps | Queries with low scores — topics likely missing from your corpus |
| Most Referenced Sources | Which documents get retrieved most, with average relevance score |
| Slowest Queries | Top 10 by latency — useful for spotting embed API bottlenecks |

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
make ingest-force
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

## Makefile shortcuts

```bash
make up              # docker compose up -d
make down            # docker compose down
make restart         # rebuild and restart mcp-server only
make logs            # tail mcp-server logs
make build           # build both images locally
make ingest          # ingest new PDFs (skips already-ingested)
make ingest-force    # re-ingest all PDFs
make watch           # start continuous watch mode (auto-ingest on PDF drop)
make watch-stop      # stop the watch container
make gen-sidecars    # auto-generate JSON sidecars via gpt-4o-mini
make stats           # open stats page in browser (macOS)
```

Pass extra args via `ARGS`:
```bash
make ingest ARGS="/docs/cisco-ios-xe-17.pdf"
make gen-sidecars ARGS="--force"   # overwrite existing sidecars
```

---

## CI/CD

| Workflow | Trigger | What it does |
|---|---|---|
| CI | Every PR + push to `main` | ruff lint + format check, Docker build (no push) |
| Release | Push to `main` or `v*` tags | Builds and pushes images to GHCR, auto-deploys to server |

**GHCR images:**
```
ghcr.io/afly007/rag-docs/mcp-server:latest
ghcr.io/afly007/rag-docs/mcp-server:v1.3.0   # pinned release tags
ghcr.io/afly007/rag-docs/ingest:latest
```

Images are public — no login required to pull.

**Automated deploy:** merges to `main` trigger the release workflow, which builds and pushes new images to GHCR and then deploys via a self-hosted GitHub Actions runner on the server. Manual fallback:

```bash
cd ~/rag-docs
docker compose pull mcp-server
docker compose up -d mcp-server
```

---

## Development

**Prerequisites:** Python 3.12+, [pipx](https://pipx.pypa.io/), Docker

```bash
# Install pre-commit hooks (runs ruff on every commit)
make pre-commit-install   # requires: sudo apt install pipx && pipx ensurepath
```

All changes go through pull requests — direct pushes to `main` are blocked. The CI workflow must pass (lint + build) before merging.

```bash
git checkout -b feat/your-feature
# make changes
ruff check --fix . && ruff format .   # fix lint before committing
git add <files> && git commit -m "feat: description"
git push -u origin feat/your-feature
gh pr create --title "..." --body "..."
```

ruff is configured in `pyproject.toml` (`line-length=100`, Python 3.12, rules E/F/W/I/UP).

---

## Switching to local embeddings

To eliminate OpenAI API costs, swap `text-embedding-3-small` for a local model (e.g. `nomic-embed-text` via Ollama). The embedding dimension changes from 1536 to 768, so the existing Qdrant collection must be deleted and re-created before re-ingesting. Update `EMBEDDING_MODEL` and `EMBEDDING_DIM` in both `ingest/ingest.py` and `mcp-server/server.py`.
