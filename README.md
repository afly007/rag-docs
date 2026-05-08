# Network Docs RAG

A self-hosted RAG pipeline that ingests vendor PDF documentation, internal Markdown files, and curated web content into Qdrant and exposes `search_docs`, `search_community`, and `list_docs` tools via MCP server, allowing Claude to retrieve relevant CLI syntax, configuration examples, and design references mid-conversation.

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 Remote Server                   │
│                                                 │
│  ┌──────────┐      ┌──────────────────────────┐ │
│  │  Qdrant  │◄─────│  MCP Server  :8000       │ │
│  │  :6333   │      │  /sse  — search_docs()   │ │
│  └──────────┘      │         search_community()│ │
│        ▲           │         list_docs()      │ │
│  ┌─────┴──────────────────────────┐  /stats   │ │
│  │  Ingest (one-shot containers)  │            │ │
│  │  ./docs/*.pdf  → chunks →      │            │ │
│  │  ./docs/*.md   → chunks →      │            │ │
│  │  URL manifest  → chunks →      │            │ │
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
| `mcp-server` | `./mcp-server` | FastMCP over SSE — exposes `search_docs`, `search_community`, and `list_docs` tools, stats dashboard, query log |
| `ingest` | `./ingest` | One-shot ingestion of PDFs and Markdown files (run manually, profile-gated) |
| `ingest-watch` | `./ingest` | Continuous watch mode — polls `./docs/` every 30s and ingests new PDFs and `.md` files automatically (profile-gated) |

**Embeddings:** OpenAI `text-embedding-3-small` (1536 dims, cosine similarity)
**Search:** Hybrid — dense vector + BM25 sparse (tiktoken TF · Qdrant IDF), fused with Reciprocal Rank Fusion, optional cross-encoder re-ranking
**Chunking:** Section-aware — splits at PDF TOC boundaries so CLI blocks and tables stay intact; heading-boundary splitting for Markdown; falls back to fixed 750-token stride for docs with no TOC/headings
**Trust tiers:** Every chunk carries a `trust_tier` (1–4) and `source_type` field — community content (tier 4) is excluded from `search_docs()` by default
**Persistent volumes:** `qdrant_data` (vectors), `mcp_data` (query log SQLite DB)

---

## Trust Tiers

Every ingested chunk carries two trust fields:

| Tier | `source_type` | Examples | Searchable via |
|---|---|---|---|
| 1 | `vendor-doc` | Cisco CLI reference, Juniper config guide, Arista EOS docs | `search_docs()` |
| 2 | `validated-design` | HPE Aruba VSDs, Cisco CVDs, reference architectures | `search_docs()` |
| 3 | `internal` | SE team runbooks, internal design notes (Markdown) | `search_docs()` |
| 4 | `community` | Curated Reddit posts, blog articles, web pages | `search_community()` only |

Tier 4 content is **excluded by default** from `search_docs()`. It is only returned when explicitly calling `search_community()`, which always prepends a caveat block. Tiers 1–3 are returned together in `search_docs()`, with a mixed-tier preamble when results span multiple tiers.

Set `trust_tier` and `source_type` in sidecar `.json` files (for PDFs and Markdown) or in the web manifest (for URLs). The `make gen-sidecars` command auto-detects tier 2 for CVDs and VSDs.

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

# Optional — tune community result penalty (default 0.75 = 25% score penalty)
# TIER_BOOST_4=0.75
```

### 2. Start Qdrant and MCP server

```bash
docker compose up -d
```

Both `qdrant_data` and `mcp_data` are Docker volumes — they survive container restarts and rebuilds.

### 3. Add documents and ingest

#### PDFs

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

#### Markdown files

Drop `.md` files anywhere under `./docs/`. They are ingested alongside PDFs by `make ingest`. Chunks are split at heading boundaries (`#`, `##`, `###`), falling back to fixed-stride for files with no headings.

Create a sidecar `.json` next to each `.md` file to tag it:

```
docs/
  se-team/
    bgp-design-notes.md
    bgp-design-notes.json    ← trust_tier=3, source_type=internal
```

#### Web pages (community tier)

Create a JSON manifest listing URLs to fetch:

```json
[
  {
    "url": "https://example.com/aruba-lag-config",
    "vendor": "aruba",
    "product": "aos-cx",
    "last_updated": "2024-06-01"
  },
  {
    "url": "https://example.com/ospf-tuning-tips"
  }
]
```

All entries default to `trust_tier=4, source_type=community` unless overridden. Text is extracted with trafilatura and chunked by Markdown headings. Run:

```bash
make ingest-web ARGS="/docs/community.json"
```

---

### Tagging documents with metadata

#### Option A — Auto-generate sidecars (recommended for PDFs)

```bash
make gen-sidecars
```

This calls gpt-4o-mini on the first 10 pages of each PDF to extract vendor, product, version, doc_type, trust_tier, and source_type, then writes a draft `.json` sidecar alongside each PDF. It automatically classifies CVDs, Validated Solution Guides, and reference architectures as tier 2. Review and edit the files before ingesting.

#### Option B — Write sidecars manually

For each PDF or Markdown file, create a sidecar `.json` with the same base name:

```
docs/
  cisco-ios-xe-17.pdf
  cisco-ios-xe-17.json                ← sidecar
  aruba-vsd-campus.pdf
  aruba-vsd-campus.json               ← tier 2 sidecar
  se-team/design-notes.md
  se-team/design-notes.json           ← tier 3 sidecar
```

Sidecar format:
```json
{
  "vendor":      "cisco",
  "product":     "ios-xe",
  "version":     "17.9.1",
  "doc_type":    "cli-reference",
  "trust_tier":  1,
  "source_type": "vendor-doc"
}
```

For a validated design guide:
```json
{
  "vendor":      "hpe",
  "product":     "aos-cx",
  "version":     "10.13",
  "doc_type":    "validated-design",
  "trust_tier":  2,
  "source_type": "validated-design"
}
```

For internal documentation:
```json
{
  "author":      "se-team",
  "doc_type":    "design-guide",
  "trust_tier":  3,
  "source_type": "internal"
}
```

Common `doc_type` values: `cli-reference`, `config-guide`, `design-guide`, `validated-design`, `release-notes`, `white-paper`

Without a sidecar the document is still ingested and searchable — metadata just won't be available for filtering. You can add sidecars later and re-ingest with `make ingest-force` to backfill.

---

### Backfilling trust tiers on existing collections

If you have an existing collection ingested before trust tiers were introduced, run the backfill script once to tag all existing chunks as `trust_tier=1 / source_type=vendor-doc`:

```bash
make backfill-tiers
```

This uses Qdrant's `set_payload` in-place — no re-embedding required.

---

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

Instead of running `make ingest` manually after every file drop, start the watch container:

```bash
make watch
```

This starts `ingest-watch` in the background. It polls `./docs/` every 30 seconds and ingests any PDF or Markdown file not yet in the collection. Drop a file and it will be searchable within 30 seconds.

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

Three MCP tools are available:

### `list_docs()`

Returns the full document catalog — vendors, products, versions, doc types, trust tiers, and chunk counts. Call this first to discover what filter values are available.

```
You: What documentation do you have access to?

Claude: [calls list_docs()]
        → Collection: network_docs  Documents: 14  Chunks: 52,108

        Available vendors:      arista, cisco, hpe, juniper
        Available products:     aos-cx, eos, ios-xe, junos
        Available source_types: internal, validated-design, vendor-doc

        Document                                      Vendor    Product   Version  Doc Type          Tier   Chunks
        ──────────────────────────────────────────────────────────────────────────────────────────────────────────
        cisco-ios-xe-17-cli.pdf                       cisco     ios-xe    17.9.1   cli-reference     1      4,209
        aruba-vsd-campus-10.13.pdf                    hpe       aos-cx    10.13    validated-design  2      1,847
        se-team/bgp-design-notes.md                   —         —         —        design-guide      3        142
        ...

        Trust tiers: 1=vendor-doc (authoritative)  2=validated-design  3=internal  4=community
        Community (tier-4) docs are excluded from search_docs() — use search_community() to query them.
```

### `search_docs(query, vendor, product, doc_type, version, source_type)`

Searches vendor-official (tier 1), validated-design (tier 2), and internal (tier 3) content using hybrid retrieval (BM25 + dense vectors, RRF fused) and returns the top 5 matching chunks with surrounding context. All filter arguments are optional. Community content (tier 4) is always excluded.

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
search_docs("campus core design", source_type="validated-design")
search_docs("spanning-tree port-priority", version="10.16")
```

When results span multiple tiers (e.g., tier 1 and tier 2), a preamble block lists the count per tier before the results.

### `search_community(query, vendor, product, doc_type)`

Searches **only** tier-4 community content (curated web pages, blog posts). Always prepends a caveat block reminding you to verify findings against vendor documentation.

```
You: Are there any community notes on AOS-CX OSPF tuning?

Claude: [calls search_community("AOS-CX OSPF tuning", product="aos-cx")]
        → COMMUNITY SOURCES — tier 4. Results from curated community content ...
        → returns top 5 community chunks with URLs
```

**Prompting tip:** Claude won't use MCP tools unless the question makes it obvious. Phrases like "search my network docs for…", "according to the AOS-CX documentation…", or "check community references for…" reliably trigger tool calls.

**Note:** Filtering only works on documents ingested with a sidecar `.json` file or web manifest metadata. Documents without metadata are always included in unfiltered searches.

---

## Stats dashboard

Open `http://YOUR_SERVER_IP:8000/stats` in a browser. Auto-refreshes every 60 seconds.

**Cards:** Documents · Total Chunks · Queries Today · Total Queries · Avg Latency · Avg Score

**Sections:**

| Section | What it shows |
|---|---|
| Document Catalog | All ingested documents with vendor, product, version, doc type, trust tier (colour-coded), page and chunk counts |
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
  'SELECT ts, query, top_score, top_source, top_source_type FROM queries ORDER BY id DESC LIMIT 20'"
```

### Delete and re-ingest a collection
```bash
curl -X DELETE http://localhost:6333/collections/network_docs
make ingest-force
```

### Backfill trust tiers on an existing collection
```bash
make backfill-tiers
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
make ingest          # ingest new PDFs and Markdown files (skips already-ingested)
make ingest-force    # re-ingest all PDFs and Markdown files
make ingest-web      # ingest web pages from a JSON manifest (ARGS="/docs/community.json")
make backfill-tiers  # tag existing chunks with trust_tier=1/source_type=vendor-doc (run once after upgrade)
make watch           # start continuous watch mode (auto-ingest on PDF or .md drop)
make watch-stop      # stop the watch container
make gen-sidecars    # auto-generate JSON sidecars via gpt-4o-mini
make stats           # open stats page in browser (macOS)
```

Pass extra args via `ARGS`:
```bash
make ingest ARGS="/docs/cisco-ios-xe-17.pdf"
make ingest-web ARGS="/docs/community.json"
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
