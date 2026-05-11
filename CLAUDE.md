# CLAUDE.md — Distill

Context for AI assistant sessions working in this repo.

## What this project is

A self-hosted RAG pipeline that ingests technical PDFs (vendor docs, internal guides, curated web pages) into Qdrant and exposes `search_docs()`, `search_community()`, and `list_docs()` tools via a FastMCP SSE server. Any MCP-compatible client (Claude Code CLI via native SSE, Claude Desktop via `mcp-remote`, Cursor, Windsurf, etc.) can connect.

Runs on a remote Ubuntu server with 750 GB RAM at `192.168.0.50`. The user is a network architect/engineer primarily indexing Cisco, Juniper, and Arista/Aruba documentation.

## Repo layout

```
docker-compose.yml       — Qdrant + mcp-server + ingest (ingest is profile-gated)
mcp-server/
  server.py              — FastMCP SSE server, search_docs, list_docs, stats page, SQLite log, /clip endpoint
  requirements.txt
ingest/
  ingest.py              — PDF → chunks → embeddings → Qdrant
  requirements.txt
browser-extension/
  manifest.json          — MV3, permissions: activeTab, storage, tabs
  popup.html/js          — One-click save UI; auto-rewrites reddit.com → old.reddit.com
  options.html/js        — Server URL + API key settings; test connection button
docs/                    — PDFs and JSON sidecars (gitignored, managed on server)
scripts/deploy.sh        — Manual deploy helper
pyproject.toml           — ruff config
Makefile                 — Common tasks (see `make help`)
```

## Architecture decisions

**AsyncOpenAI is required.** The sync client blocks the asyncio event loop, which starves the SSE keepalive and causes `Tool result could not be submitted` errors in Claude. Do not revert to `openai.OpenAI`.

**mcp 1.27.0 uses the public API.** We previously hacked into FastMCP internals (`mcp._mcp_server`, `SseServerTransport`) to add a custom `/stats` route. This is no longer needed — use `@mcp.custom_route("/stats", methods=["GET"])` and `mcp.sse_app()` instead.

**UPSERT_BATCH=200 in ingest.** Qdrant rejects payloads over 32 MB. Batching at 200 points keeps each request well under the limit.

**SQLite WAL mode + threading.Lock.** The stats page renders synchronously while MCP tool calls run async. All SQLite access goes through `_db_lock` to prevent write conflicts.

**Qdrant is the source of truth for duplicate detection.** `already_ingested()` scrolls Qdrant for the source filename rather than maintaining a separate manifest.

**Re-ranking is opt-in via `RERANKER` env var.** `init_reranker()` is called in `main()` and sets the module-level `_reranker`. `rerank_hits()` is a no-op when `_reranker is None`. When enabled, `search_docs()` fetches `PREFETCH_K=20` candidates from Qdrant (instead of `TOP_K=5`) and the re-ranker picks the top 5. flashrank model names must match `Config.model_file_map` exactly — `ms-marco-MiniLM-L-12-v2` is correct; `ms-marco-MiniLM-L-6-v2` (from sentence-transformers) does not exist in flashrank and causes a 404 on model download.

**Hybrid search uses BM25 sparse + dense vectors with RRF fusion.** Each chunk is stored with two vectors: a dense embedding (`text-embedding-3-small`) and a BM25 sparse vector built from tiktoken token frequencies with Qdrant server-side IDF. At query time, `query_points` runs both retrievers as `Prefetch` branches and fuses results with Reciprocal Rank Fusion. Use `FusionQuery(fusion=Fusion.RRF)` — NOT `Fusion.RRF` directly — as qdrant-client 1.17.1 serialises the bare enum as the string `"rrf"` which the REST API rejects with 400.

**Section-aware chunking uses the PDF's TOC as split boundaries.** `extract_toc_sections()` calls `doc.get_toc(simple=False)` to get bookmark entries with y-coordinates, then computes end boundaries by scanning forward for the next entry at the same or higher level. Only leaf sections (`has_children=False`) are chunked — parent headings are skipped because their text appears in child sections. Sections below `MIN_SECTION_TOKENS=100` are merged forward into the next section. Large sections use the same 750/100 sliding window, scoped within the section. Chunk IDs are `uuid5(NAMESPACE_URL, f"{source}:{section_title}:{sub_chunk_n}")` — stable to page reflow, version-correct when titles change. New payload fields: `section_title`, `section_level`, `section_index`. Falls back to fixed-stride when `get_toc()` returns empty. `delete_source_chunks()` is called on `--force` re-ingest so orphaned old-ID chunks are removed before new ones are upserted. Run `scripts/test_section_detect.py <pdf>` inside the ingest container to inspect TOC quality without touching Qdrant or OpenAI.

**Browser extension clip endpoint (`/clip` + `/clip/meta`).**
- `POST /clip` — CORS-enabled, Bearer token auth (`CLIP_API_KEY`), fetches the URL server-side via `_clip_fetch()`, chunks with `_clip_chunk()`, embeds, and upserts as trust_tier=4 community content. trafilatura runs in a thread pool executor (sync library in async server). Three-pass extraction: (1) strict trafilatura, (2) lenient `favor_recall=True`, (3) raw HTML tag strip — returning `None` only if all three yield < 200 chars.
- `GET /clip/meta` — scrolls full Qdrant collection (payload-only, no vectors) and returns sorted distinct `vendor` and `product` values for the extension's `<datalist>` dropdowns. Fast even on large collections since vectors are excluded.
- `_clip_chunk()` splits on markdown headings first, then applies 750/100 sliding window per section. **Critical:** content before the first heading must be captured as a preamble section — if omitted, pages where a heading appears only near the end silently drop almost all content.
- Reddit (`www.reddit.com`) serves JS-rendered HTML that trafilatura can't parse. The popup rewrites any `www.reddit.com`, `new.reddit.com`, or `sh.reddit.com` URL to `old.reddit.com` before POSTing to `/clip`. This is done client-side in popup.js — the server receives and fetches the `old.reddit.com` URL.
- `chrome.tabs.create()` requires the explicit `"tabs"` permission in Firefox MV3, even when opening your own extension page via `chrome.runtime.getURL()`. Without it the call silently does nothing.

**Watch mode polls DOCS_DIR every 30 seconds.** `watch_loop()` in `ingest.py` is triggered by `--watch`. It calls `already_ingested()` per file and catches per-file exceptions so a bad PDF doesn't kill the loop — it retries on the next cycle. The `ingest-watch` compose service (profile: `watch`) runs with `restart: unless-stopped`. Start with `make watch`, stop with `make watch-stop`.

**Vendor aliases expand search across acquisition name variants.** `_VENDOR_ALIASES` in `server.py` maps equivalent names (e.g. `aruba`, `hpe`, `hewlett-packard-enterprise`, `arubanetworks`) to the same group. `build_filter()` uses `MatchAny` instead of `MatchValue` so a user searching `vendor=aruba` finds docs tagged `hewlett-packard-enterprise` by gen-sidecars. Add new alias groups when vendor naming is ambiguous.

## Critical constraints

### mcp-remote requires --allow-http

The Claude Desktop config must include `--allow-http` in the args array or mcp-remote will refuse non-HTTPS non-localhost URLs:

```json
{
  "mcpServers": {
    "distill": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://192.168.0.50:8000/sse", "--allow-http"]
    }
  }
}
```

### GHCR image path includes repo name

The `IMAGE_BASE` in `.env` must be `ghcr.io/afly007/distill` (not just `ghcr.io/afly007`). The release workflow uses `ghcr.io/${{ github.repository }}` which expands to the full `owner/repo` path.

GHCR package visibility is independent of repo visibility. Even with a public repo, packages default to private and must be made public manually at:
- `https://github.com/users/afly007/packages/container/distill%2Fmcp-server/settings`
- `https://github.com/users/afly007/packages/container/distill%2Fingest/settings`

## Git workflow

Branch protection is enforced on `main` — direct pushes are rejected. All changes go through PRs.

```bash
git checkout -b feat/your-feature
# make changes
git add <files>
git commit -m "feat: description"
git push -u origin feat/your-feature
gh pr create --title "..." --body "..."
gh pr checks <number> --watch          # wait for lint + build
gh pr merge <number> --squash --delete-branch
git checkout main && git pull --rebase origin main
```

Workflow files (`.github/workflows/`) require the `workflow` OAuth scope to merge via `gh pr merge`. If that fails, merge in the browser.

## CI/CD

**CI** (`ci.yml`) — runs on all PRs and pushes to `main`:
- Lint: `ruff check .` + `ruff format --check .`
- Build: Docker build of both images (no push)

**Release** (`release.yml`) — runs on push to `main` and `v*` tags:
- Builds and pushes `mcp-server` and `ingest` images to GHCR
- On `v*` tags, also pushes a `:vX.Y.Z` tag alongside `:latest` and `:<sha>`

## Deploy

Merges to `main` auto-deploy via the self-hosted GitHub Actions runner (`~/actions-runner` on the server). The runner runs as a systemd service (`actions.runner.afly007-distill.distill-server`).

Manual fallback:
```bash
cd ~/distill
docker compose pull mcp-server
docker compose up -d mcp-server
docker compose logs -f mcp-server
```

Expected startup log:
```
INFO Connected to Qdrant
INFO Uvicorn running on http://0.0.0.0:8000
```

## SQLite schema migration

`init_db()` uses `CREATE TABLE IF NOT EXISTS` (no-op on existing tables) followed by a `PRAGMA table_info` check that `ALTER TABLE ADD COLUMN`s any missing columns. When adding new columns to the `queries` table, add them to both the `CREATE TABLE` statement and the migration loop:

```python
for col in ("vendor", "product", "doc_type", "your_new_col"):
    if col not in existing:
        conn.execute(f"ALTER TABLE queries ADD COLUMN {col} TEXT")
```

## Linting

`ruff` is configured in `pyproject.toml`:
- `line-length = 100`
- `target-version = "py312"`
- Rules: E, F, W, I (imports), UP (pyupgrade)
- `print()` is allowed in `ingest/ingest.py` and `mcp-server/server.py`

Run before committing:
```bash
ruff check --fix . && ruff format .
```

Pre-commit hooks run ruff automatically on `git commit` (requires `pipx install pre-commit && pre-commit install` — not `pip install`, due to PEP 668).

## Common pitfalls encountered

| Symptom | Cause | Fix |
|---|---|---|
| `Tool result could not be submitted` in MCP client | Sync OpenAI client blocked event loop | Use `AsyncOpenAI`, never `OpenAI` |
| `sqlite3.OperationalError: no such column: vendor` | Old DB schema on persistent volume | Migration in `init_db()` adds missing cols |
| `unsupported format string passed to NoneType.__format__` | `dict.get(key, default)` returns `None` when key exists with None value | Use `value or '—'` not `dict.get(key, '—')` |
| Stats page 500 on fresh deploy | Pre-existing `queries.db` missing new columns | Same — migration handles it |
| `pull access denied` for GHCR | Package is private or wrong IMAGE_BASE | Make package public; set `IMAGE_BASE=ghcr.io/afly007/distill` |
| SSE `terminated: other side closed` after ~6 min idle | Router/firewall killing idle TCP connections | Fixed — SSE ping=30s injected in `main()` before `sse_app()` |
| `Received request before initialization was complete` | mcp-remote replayed tool calls on a new session before MCP handshake | Start a fresh conversation |
| Dependabot PRs failing lint | Our code had lint errors before Dependabot ran | Fix lint on `main` first, then `@dependabot rebase` |
| Hybrid search returns 400 Bad Request | `Fusion.RRF` serialises as `"rrf"` (bare string) but API expects `{"fusion":"rrf"}` | Use `FusionQuery(fusion=Fusion.RRF)` in `query_points` call |
| Collection migration required for hybrid search | Old collection has unnamed dense vector; hybrid needs named `dense` + sparse `bm25` | `curl -X DELETE http://localhost:6333/collections/distill` then `make ingest-force` |
| `RERANKER=local` fails with 404 on model download | Wrong model name — flashrank model names differ from sentence-transformers | Use `ms-marco-MiniLM-L-12-v2` not `ms-marco-MiniLM-L-6-v2`; valid names are in `flashrank.Config.model_file_map` |
| Clip returns 1 chunk for most pages | `_clip_chunk()` heading path only captured text *from* headings, silently dropping all content before the first heading | Fixed — preamble section added before first heading; always verify with `docker exec mcp-server python3 -c "import trafilatura; ..."` |
| Extension settings link does nothing in Firefox | `chrome.tabs.create()` silently fails without the `"tabs"` permission in Firefox MV3 | Add `"tabs"` to `permissions` array in `manifest.json` |
| Clip returns "No extractable text" | Page is JS-rendered (React/Angular SPA) or has bot protection | Use three-pass fallback in `_clip_fetch()`; for true SPAs a headless browser would be required |
| Reddit clips have near-empty content | `www.reddit.com` serves JS-rendered HTML | popup.js auto-rewrites to `old.reddit.com`; already-indexed bad clips must be deleted via Qdrant filter DELETE API |
| `vendor=aruba` filter returns no results | gen-sidecars tags HPE docs as `hewlett-packard-enterprise` | `_VENDOR_ALIASES` + `MatchAny` in `build_filter()` handles this — add new alias groups for other ambiguous vendor names |

## Embedding model

`text-embedding-3-small` · 1536 dimensions · cosine similarity

Changing the model requires:
1. Delete the Qdrant collection: `curl -X DELETE http://localhost:6333/collections/distill`
2. Update `EMBEDDING_MODEL` and `EMBEDDING_DIM` in both `ingest/ingest.py` and `mcp-server/server.py`
3. Re-ingest all documents

## Security posture

This server runs on a private LAN (`192.168.0.50`) — network isolation is the primary perimeter. Known gaps tracked as GitHub issues.

**What is protected:**
- `/clip` and `/clip/meta` require `Bearer CLIP_API_KEY` header
- Qdrant write access only via the mcp-server container (no external writes possible without LAN access)
- Query log (SQLite) is inside a Docker volume, not externally accessible

**What is NOT protected (see issues for fixes):**
- MCP SSE endpoint (`/sse`) has no authentication — any LAN host can call all MCP tools
- `/stats` page is unauthenticated — exposes document catalog and query history
- Qdrant port 6333 is bound to the host — any LAN host can read/write/delete the collection directly
- `/clip` fetches any URL without SSRF protection — can probe internal services
- No rate limiting on `/clip` — OpenAI credits can be exhausted by bulk requests
- All traffic is plaintext HTTP — API keys travel unencrypted on the LAN
- Browser extension `host_permissions` is `["http://*/*", "https://*/*"]` — broader than needed
- CORS on `/clip` is `Allow-Origin: *` — any page can trigger clip requests if key is known

## Pending work

Priority-ordered. Items marked **quality** improve search results; **infra** are maintenance/reliability.

### Quality improvements

| Priority | Feature | Notes |
|---|---|---|
| 1 | ~~**Hybrid search** (BM25 + dense vector)~~ | ✅ Done — shipped in v1.2.0. BM25 sparse vectors via tiktoken + Qdrant IDF, fused with RRF. |
| 2 | ~~**Auto-sidecar generation**~~ | ✅ Done — `ingest/gen_sidecar.py` calls gpt-4o-mini on first 10 pages, writes draft `.json` sidecar. Run via `make gen-sidecars`. |
| 3 | ~~**Re-ranking**~~ | ✅ Done — shipped in v1.3.0. `RERANKER=local` uses flashrank ms-marco-MiniLM-L-12-v2 (ONNX, ~22 MB, no PyTorch). `RERANKER=cohere` uses Cohere Rerank API. Off by default. |
| 4 | ~~**Section-aware chunking**~~ | ✅ Done — `doc.get_toc(simple=False)` drives splits; leaf-only, merge-forward for short sections, 750/100 window within each section. New payload fields: `section_title`, `section_level`, `section_index`. Fallback to fixed-stride when no TOC. |
| 5 | ~~**Auto-ingest watch**~~ | ✅ Done — `--watch` flag on `ingest.py` polls `DOCS_DIR` every 30s. `ingest-watch` compose service (profile: `watch`). `make watch` / `make watch-stop`. |
| 6 | ~~**Browser extension clipper**~~ | ✅ Done — shipped in v1.5.0. MV3 extension in `browser-extension/`. One-click save any page to community tier. `/clip` endpoint on server. Auto-rewrites Reddit to `old.reddit.com`. Vendor/product dropdowns from `/clip/meta`. |

### Infrastructure

| Priority | Feature | Notes |
|---|---|---|
| 1 | ~~**openai 1→2 migration**~~ | ✅ Done — upgraded to `openai==2.36.0`, removed `httpx<0.28.0` pin. No code changes needed; embeddings + chat completions API is identical in 2.x. |
| 2 | ~~**Deploy secrets**~~ | ✅ Done — self-hosted runner on server, `GHCR_TOKEN` secret configured. Merges to main auto-deploy. |
| 3 | ~~**SSE keepalive**~~ | ✅ Done — `functools.partial(EventSourceResponse, ping=30)` injected before `sse_app()`. Sends SSE comment pings every 30s to reset router idle timers. |
| 4 | ~~**PR #3**~~ (`docker/build-push-action 5→7`) | ✅ Merged. |
