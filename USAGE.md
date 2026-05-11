# Usage Guide

Operational guide for day-to-day use of Distill. For initial setup and configuration see [CONFIGURATION.md](CONFIGURATION.md).

---

## Contents

- [Adding documents](#adding-documents)
  - [Vendor PDFs](#vendor-pdfs)
  - [Internal Markdown notes](#internal-markdown-notes)
  - [Curated web pages](#curated-web-pages)
  - [Browser extension](#browser-extension)
  - [File browser](#file-browser)
  - [Auto-ingest watch](#auto-ingest-watch)
- [Talking to your AI](#talking-to-your-ai)
- [What documents do I have?](#what-documents-do-i-have)
- [Inspecting ingestion quality](#inspecting-ingestion-quality)
- [Upgrading an existing collection](#upgrading-an-existing-collection)
- [Day-to-day operations](#day-to-day-operations)
- [Managing multiple document sets](#managing-multiple-document-sets)
- [Stats dashboard](#stats-dashboard)
- [Metadata reference](#metadata-reference)
- [Development](#development)
- [CI/CD](#cicd)
- [Switching to local embeddings](#switching-to-local-embeddings)

---

## Adding documents

### Vendor PDFs

Drop them into `./docs/` and run:

```bash
make ingest
```

Progress is shown per file:

```
────────────────────────────────────────────────────────────
File:  cisco-ios-xe-17.pdf  (42.3 MB)
Pages: 1847 total, 1831 with text
Chunks: 4209  (43 embedding batches)
Done:  4209 chunks stored in 23.1s
```

To help your AI filter by vendor, product, or version, add a small metadata file next to each PDF. You can write it manually or generate it automatically:

```bash
make gen-sidecars   # scans each PDF with GPT-4o-mini and writes a draft .json
```

Review and edit the generated files before re-ingesting. They look like this:

```json
{
  "vendor":   "cisco",
  "product":  "ios-xe",
  "version":  "17.9.1",
  "doc_type": "cli-reference"
}
```

`gen-sidecars` also automatically detects validated design guides (CVDs, VSDs, reference architectures) and tags them accordingly. See [Metadata reference](#metadata-reference) for the full format.

Ingestion is idempotent — re-running on the same file is safe.

### Internal Markdown notes

Your team's runbooks, design decisions, and internal guides are valuable context. Drop `.md` files into `./docs/` (in any subfolder) and run `make ingest`. They are chunked by heading boundaries, falling back to fixed-stride for files without headings.

Add a sidecar to tag them as internal:

```json
{
  "doc_type":    "design-guide",
  "source_type": "internal"
}
```

### Curated web pages

For blog posts or forum threads you've found genuinely useful, create a manifest file:

```json
[
  {
    "url": "https://example.com/ospf-tuning-tips",
    "vendor": "aruba",
    "product": "aos-cx",
    "last_updated": "2024-06-01"
  }
]
```

```bash
make ingest-web ARGS="/docs/community.json"
```

These are stored as community-tier content and only surface when you explicitly ask for them.

### Browser extension

The clipper extension lets you save any page you're reading directly to your Distill server — no manifest files, no copy-pasting URLs.

**Setup:**

1. Add `CLIP_API_KEY` to your `.env` (generate one with `openssl rand -hex 32`) and restart the server: `make restart`
2. In Chrome/Edge, go to `chrome://extensions`, enable **Developer mode**, click **Load unpacked**, and select the `browser-extension/` folder
3. Click the extension icon, open **⚙ Settings**, enter your server URL and API key, click **Test connection**

Firefox: go to `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on** → select `browser-extension/manifest.json`.

**Using it:**

Browse to any page you want to save, click the extension icon, optionally tag it with a vendor and product (the dropdowns are pre-populated from your collection), and click **Save to Distill**. The extension captures the already-rendered DOM and sends it to the server for chunking and embedding — this means JavaScript-rendered pages (vendor portals, SPAs like arista.com) clip correctly without a headless browser. A green confirmation shows the chunk count when done.

Reddit links are automatically redirected to `old.reddit.com` for better text extraction — a blue notice in the popup confirms the rewrite.

Saved pages are immediately searchable via `search_community()`. They won't appear in `search_docs()` results (community content is always opt-in).

### File browser

Open `http://YOUR_SERVER_IP:8000/files` in a browser for a web-based document management UI — no SSH required.

| Action | How |
|---|---|
| Upload | Drag-and-drop or click to select `.pdf` or `.md` files. Files are ingested immediately. |
| Auto-metadata (✦) | PDF rows only. Scans the first 10 pages with GPT-4o-mini and pre-fills the metadata form for review. |
| Edit metadata (✎) | Opens the sidecar edit modal. Check **Re-ingest after saving** to immediately re-index with new metadata. |
| Download (↓) | Downloads the original file. |
| Delete (✕) | Removes the file, its sidecar, and all associated Qdrant chunks. |

Columns are sortable — click any header to sort ascending, click again to reverse.

> If `COMPOSE_PROFILES=tls` and `ADMIN_USER` is set, the `/files` page requires basic auth credentials.

### Auto-ingest watch

To ingest new files automatically as you drop them in:

```bash
make watch        # starts background watcher — polls ./docs/ every 30s
make watch-stop   # stop it
```

---

## Talking to your AI

Any MCP-compatible assistant uses the search tool automatically when it recognises that your question is about your documentation. A few phrases that reliably trigger it:

- *"Search the docs for…"*
- *"According to the AOS-CX documentation, how do I…"*
- *"Using the docs, what's the correct syntax for…"*
- *"Check the Juniper config guide for…"*

**What works well:**
- CLI syntax questions — *"What's the command to configure LACP on AOS-CX?"*
- Configuration examples — *"Show me how to set up OSPF area types on IOS-XE"*
- Design tradeoffs — *"What does the validated design recommend for core redundancy?"*
- Version-specific questions — *"Is this BGP syntax valid in JunOS 23.2?"*

**What doesn't work:**
- Asking about topics not in your documents — the AI will say nothing relevant was found rather than guessing
- Very short or ambiguous queries — give it enough to search with

### Sample conversation

```
You:    Search the docs for how to configure BGP route reflectors on IOS-XE

AI:     [calls search_docs("BGP route reflector configuration", vendor="cisco", product="ios-xe")]

        [1] cisco-ios-xe-17-cli.pdf  |  page 847  |  §BGP Route Reflector  |  [VENDOR-DOC tier-1]

        To configure a route reflector:

          router bgp 65000
           bgp cluster-id 1
           neighbor 10.0.0.2 remote-as 65000
           neighbor 10.0.0.2 route-reflector-client

        The cluster-id is optional when there is only one route reflector in the cluster...
```

### Asking for community references

Community sources are opt-in. Ask for them explicitly and the AI will always flag them as unverified:

```
You:    Are there any community notes on AOS-CX OSPF tuning?

AI:     [calls search_community("AOS-CX OSPF tuning", product="aos-cx")]

        COMMUNITY SOURCES — tier 4. Results from curated community content.
        Verify against vendor documentation before implementing in production.

        [1] score 0.412  |  vendor=aruba  |  product=aos-cx
            URL: https://example.com/ospf-tuning-tips
            ...
```

---

## What documents do I have?

Ask your AI directly or check the live dashboard:

```
You:    What documentation do you have access to?

AI:     [calls list_docs()]

        Collection: distill  |  Documents: 14  |  Chunks: 52,108

        Document                            Vendor  Product  Version  Doc Type          Tier
        ─────────────────────────────────────────────────────────────────────────────────────
        cisco-ios-xe-17-cli.pdf             cisco   ios-xe   17.9.1   cli-reference     1
        aruba-vsd-campus-10.13.pdf          hpe     aos-cx   10.13    validated-design  2
        se-team/bgp-design-notes.md         —       —        —        design-guide      3

        Tier 4 (community) content is excluded — use search_community() to query it.
```

Open `http://YOUR_SERVER_IP:8000/stats` for a live dashboard showing ingested documents, recent queries, coverage gaps, and latency.

---

## Inspecting ingestion quality

Open `http://YOUR_SERVER_IP:8000/inspect` to verify that documents were ingested correctly.

**Source list** — all ingested sources (files and clipped URLs) with chunk count, vendor, product, and doc type. Any source with **2 or fewer chunks** is flagged with a ⚠ warning — this is the primary signal for a bad ingest (e.g. trafilatura failed to extract the page body, or a PDF had no text layer). Clipped URLs (http/https) show a **✕ delete button** — clicking it removes all chunks for that source from the index after confirmation. To delete file-based documents use the `/files` page instead.

**Chunk detail view** — click any source row to see every chunk: chunk index, page number, section title (from PDF TOC), character count, and a 420-character text preview. Use this to confirm the actual extracted content makes sense.

Common causes of 1-chunk results from web clips:
- JavaScript-rendered pages (React/Angular SPAs) — trafilatura can't parse them
- Pages with aggressive bot protection
- Very short articles where the entire text fits in one chunk (this is fine)

For PDFs, very few chunks may indicate a scanned document without a text layer. Check the preview text — if it's garbled or empty, the PDF needs OCR preprocessing.

> If `COMPOSE_PROFILES=tls` and `ADMIN_USER` is set, the `/inspect` page requires basic auth credentials.

---

## Upgrading an existing collection

If you have documents ingested before trust tiers were introduced, run this once to tag them all as vendor documentation:

```bash
make backfill-tiers
```

No re-ingestion needed — it updates existing records in place.

---

## Day-to-day operations

```bash
# Stack
make up              # start Qdrant and MCP server
make down            # stop everything
make restart         # rebuild and restart MCP server after code changes
make logs            # tail MCP server logs
make build           # build both images locally

# Ingestion
make ingest          # ingest new PDFs and Markdown files (skips already-ingested)
make ingest-force    # re-ingest everything (use after editing sidecars)
make ingest-web      # ingest web pages from a manifest  ARGS="/docs/community.json"
make backfill-tiers  # tag existing chunks with trust_tier=1 (run once after upgrading)
make watch           # auto-ingest new files dropped into ./docs/ every 30s
make watch-stop      # stop the watch container

# Metadata
make gen-sidecars    # auto-generate metadata sidecars for PDFs
make stats           # open stats page in browser (macOS)
```

Pass extra args via `ARGS`:

```bash
make ingest ARGS="/docs/cisco-ios-xe-17.pdf"
make ingest-force ARGS="/docs/cisco-ios-xe-17.pdf"
make gen-sidecars ARGS="--force"   # overwrite existing sidecars
```

### View live logs

```bash
docker compose logs -f mcp-server
docker compose logs -f qdrant
```

### Query the log database directly

```bash
docker run --rm -v distill_mcp_data:/data alpine \
  sh -c "apk add -q sqlite && sqlite3 /data/queries.db \
  'SELECT ts, query, top_score, top_source, top_source_type FROM queries ORDER BY id DESC LIMIT 20'"
```

### Delete and re-ingest a collection

```bash
curl -X DELETE http://localhost:6333/collections/distill
make ingest-force
```

### Check Qdrant collection info

```bash
curl http://localhost:6333/collections/distill
```

---

## Managing multiple document sets

Set `COLLECTION_NAME` to separate doc sets into named collections:

```bash
COLLECTION_NAME=networking   docker compose --profile ingest run --rm ingest
COLLECTION_NAME=cloud        docker compose --profile ingest run --rm ingest
```

The MCP server searches whichever collection it was started with. To switch, update `.env` and restart:

```bash
docker compose up -d mcp-server
```

---

## Stats dashboard

Open `http://YOUR_SERVER_IP:8000/stats` in a browser. Auto-refreshes every 60 seconds.

| Section | What it shows |
|---|---|
| Document Catalog | All ingested documents with vendor, product, version, doc type, trust tier, page and chunk counts |
| Recent Queries | Last 30 queries — time, text, filters, score, source, latency |
| Coverage Gaps | Queries scoring below threshold — topics likely missing from your corpus |
| Most Referenced Sources | Which documents get retrieved most, with average relevance score |
| Slowest Queries | Top 10 by latency — useful for spotting embedding API bottlenecks |

Every query is persisted to `/data/queries.db` (SQLite, WAL mode) inside the `mcp_data` Docker volume and survives container restarts.

> If `COMPOSE_PROFILES=tls` and `ADMIN_USER` is set, the `/stats` page requires basic auth credentials.

---

## Metadata reference

Every chunk carries two trust fields set from the sidecar `.json` file:

| `trust_tier` | `source_type` | Searchable via | Description |
|---|---|---|---|
| 1 | `vendor-doc` | `search_docs()` | Standard vendor CLI refs, config guides, release notes |
| 2 | `validated-design` | `search_docs()` | CVDs, VSDs, reference architectures |
| 3 | `internal` | `search_docs()` | Internal team docs and runbooks |
| 4 | `community` | `search_community()` only | Curated web content — always prepends caveat |

Full sidecar format (all fields optional):

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

Common `doc_type` values: `cli-reference`, `config-guide`, `design-guide`, `validated-design`, `release-notes`, `white-paper`

Documents without sidecars are still ingested and searchable — metadata is just not available for filtering. Add sidecars and re-run `make ingest-force` to backfill.

**Design guides without a version:** leave `version` and `product` blank — missing fields match any filter, not no filter. Set `doc_type=design-guide` and `trust_tier=2` for validated designs.

**Vendor aliases:** searching `vendor=aruba` also returns documents tagged `hewlett-packard-enterprise`, `hpe`, and `arubanetworks` — the server normalises these automatically.

---

## Development

**Prerequisites:** Python 3.12+, [pipx](https://pipx.pypa.io/), Docker

```bash
# Install pre-commit hooks (runs ruff on every commit)
make pre-commit-install   # requires: sudo apt install pipx && pipx ensurepath
```

All changes go through pull requests — direct pushes to `main` are blocked.

```bash
git checkout -b feat/your-feature
# make changes
ruff check --fix . && ruff format .   # fix lint before committing
git add <files> && git commit -m "feat: description"
git push -u origin feat/your-feature
gh pr create --title "..." --body "..."
gh pr checks <number> --watch
gh pr merge <number> --squash --delete-branch
```

ruff is configured in `pyproject.toml` (`line-length=100`, Python 3.12, rules E/F/W/I/UP).

---

## CI/CD

| Workflow | Trigger | What it does |
|---|---|---|
| CI | Every PR + push to `main` | ruff lint + format check, Docker build (no push) |
| Release | Push to `main` or `v*` tags | Builds and pushes images to GHCR, auto-deploys to server |

**GHCR images** (public — no login required):

```
ghcr.io/afly007/distill/mcp-server:latest
ghcr.io/afly007/distill/ingest:latest
ghcr.io/afly007/distill/caddy:latest
```

Merges to `main` auto-deploy via the self-hosted GitHub Actions runner on the server. Manual fallback:

```bash
cd ~/distill
docker compose pull mcp-server
docker compose up -d mcp-server
```

---

## Switching to local embeddings

To eliminate OpenAI API costs, swap `text-embedding-3-small` for a local model (e.g. `nomic-embed-text` via Ollama). The embedding dimension changes from 1536 to 768, so the existing Qdrant collection must be deleted and re-created before re-ingesting. Update `EMBEDDING_MODEL` and `EMBEDDING_DIM` in both `ingest/ingest.py` and `mcp-server/server.py`.

---

*For environment variables, TLS setup, and MCP client configuration see [CONFIGURATION.md](CONFIGURATION.md).*
