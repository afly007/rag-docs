# CLAUDE.md — Network Docs RAG

Context for AI assistant sessions working in this repo.

## What this project is

A self-hosted RAG pipeline that ingests vendor network PDFs (Cisco, Juniper, Arista, etc.) into Qdrant and exposes `search_docs()` and `list_docs()` tools via a FastMCP SSE server. Claude connects to the server via `mcp-remote` (desktop app) or native SSE (Claude Code CLI).

Runs on a remote Ubuntu server with 750 GB RAM at `192.168.0.50`. The user is a network architect/engineer.

## Repo layout

```
docker-compose.yml       — Qdrant + mcp-server + ingest (ingest is profile-gated)
mcp-server/
  server.py              — FastMCP SSE server, search_docs, list_docs, stats page, SQLite log
  requirements.txt
ingest/
  ingest.py              — PDF → chunks → embeddings → Qdrant
  requirements.txt
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

**Hybrid search uses BM25 sparse + dense vectors with RRF fusion.** Each chunk is stored with two vectors: a dense embedding (`text-embedding-3-small`) and a BM25 sparse vector built from tiktoken token frequencies with Qdrant server-side IDF. At query time, `query_points` runs both retrievers as `Prefetch` branches and fuses results with Reciprocal Rank Fusion. Use `FusionQuery(fusion=Fusion.RRF)` — NOT `Fusion.RRF` directly — as qdrant-client 1.17.1 serialises the bare enum as the string `"rrf"` which the REST API rejects with 400.

## Critical constraints

### httpx pin — do not relax without upgrading openai

```
# mcp-server/requirements.txt and ingest/requirements.txt
httpx<0.28.0
openai==1.54.0
```

`openai 1.54.0` passes a `proxies` kwarg to httpx internally. httpx removed that argument in `0.28.0`, crashing both containers at startup with `TypeError: AsyncClient.__init__() got an unexpected keyword argument 'proxies'`. The pin must stay at `<0.28.0` until openai is upgraded to 2.x.

**Planned next work:** migrate to `openai>=2.0` which drops the proxies kwarg and is compatible with current httpx. This requires changes to both `ingest.py` and `server.py`.

### mcp-remote requires --allow-http

The Claude Desktop config must include `--allow-http` in the args array or mcp-remote will refuse non-HTTPS non-localhost URLs:

```json
{
  "mcpServers": {
    "network-docs": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://192.168.0.50:8000/sse", "--allow-http"]
    }
  }
}
```

### GHCR image path includes repo name

The `IMAGE_BASE` in `.env` must be `ghcr.io/afly007/rag-docs` (not just `ghcr.io/afly007`). The release workflow uses `ghcr.io/${{ github.repository }}` which expands to the full `owner/repo` path.

GHCR package visibility is independent of repo visibility. Even with a public repo, packages default to private and must be made public manually at:
- `https://github.com/users/afly007/packages/container/rag-docs%2Fmcp-server/settings`
- `https://github.com/users/afly007/packages/container/rag-docs%2Fingest/settings`

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
- SSH deploy step requires secrets: `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`, `GHCR_TOKEN` — **not yet configured**, deploy is currently manual

## Deploy

Merges to `main` auto-deploy via the self-hosted GitHub Actions runner (`~/actions-runner` on the server). The runner runs as a systemd service (`actions.runner.afly007-rag-docs.rag-docs-server`).

Manual fallback:
```bash
cd ~/rag-docs
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
| `TypeError: AsyncClient.__init__() got an unexpected keyword argument 'proxies'` | httpx≥0.28 installed | Pin `httpx<0.28.0` |
| `Tool result could not be submitted` in Claude | Sync OpenAI client blocked event loop | Use `AsyncOpenAI`, never `OpenAI` |
| `sqlite3.OperationalError: no such column: vendor` | Old DB schema on persistent volume | Migration in `init_db()` adds missing cols |
| `unsupported format string passed to NoneType.__format__` | `dict.get(key, default)` returns `None` when key exists with None value | Use `value or '—'` not `dict.get(key, '—')` |
| Stats page 500 on fresh deploy | Pre-existing `queries.db` missing new columns | Same — migration handles it |
| `pull access denied` for GHCR | Package is private or wrong IMAGE_BASE | Make package public; set `IMAGE_BASE=ghcr.io/afly007/rag-docs` |
| SSE `terminated: other side closed` after ~6 min idle | Router/firewall killing idle TCP connections | Fixed — SSE ping=30s injected in `main()` before `sse_app()` |
| `Received request before initialization was complete` | mcp-remote replayed tool calls on a new session before MCP handshake | Start a fresh Claude conversation |
| Dependabot PRs failing lint | Our code had lint errors before Dependabot ran | Fix lint on `main` first, then `@dependabot rebase` |
| Hybrid search returns 400 Bad Request | `Fusion.RRF` serialises as `"rrf"` (bare string) but API expects `{"fusion":"rrf"}` | Use `FusionQuery(fusion=Fusion.RRF)` in `query_points` call |
| Collection migration required for hybrid search | Old collection has unnamed dense vector; hybrid needs named `dense` + sparse `bm25` | `curl -X DELETE http://localhost:6333/collections/network_docs` then `make ingest-force` |

## Embedding model

`text-embedding-3-small` · 1536 dimensions · cosine similarity

Changing the model requires:
1. Delete the Qdrant collection: `curl -X DELETE http://localhost:6333/collections/network_docs`
2. Update `EMBEDDING_MODEL` and `EMBEDDING_DIM` in both `ingest/ingest.py` and `mcp-server/server.py`
3. Re-ingest all documents

## Pending work

Priority-ordered. Items marked **quality** improve search results; **infra** are maintenance/reliability.

### Quality improvements

| Priority | Feature | Notes |
|---|---|---|
| 1 | ~~**Hybrid search** (BM25 + dense vector)~~ | ✅ Done — shipped in v1.2.0. BM25 sparse vectors via tiktoken + Qdrant IDF, fused with RRF. |
| 2 | ~~**Auto-sidecar generation**~~ | ✅ Done — `ingest/gen_sidecar.py` calls gpt-4o-mini on first 10 pages, writes draft `.json` sidecar. Run via `make gen-sidecars`. |
| 3 | **Re-ranking** | Retrieve top-20 from Qdrant, re-rank to top-5 with a cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2` runs locally; Cohere Rerank API is the cloud alternative). Meaningful precision improvement when multiple docs cover the same topic. |
| 4 | **Section-aware chunking** | PyMuPDF exposes the TOC and heading levels. Chunk at section boundaries instead of fixed token count so a 750-token chunk doesn't split mid-table or mid-example. |
| 5 | **Auto-ingest watch** | `inotifywait` loop in the ingest container watches `/docs` for new `.pdf` files and ingests automatically on drop. Low effort, good quality of life. |

### Infrastructure

| Priority | Feature | Notes |
|---|---|---|
| 1 | **openai 1→2 migration** | Breaking rewrite of the Python SDK. Unblocks the `httpx<0.28.0` pin. Both `ingest.py` and `server.py` need changes. Test carefully before deploying. |
| 2 | ~~**Deploy secrets**~~ | ✅ Done — self-hosted runner on server, `GHCR_TOKEN` secret configured. Merges to main auto-deploy. |
| 3 | ~~**SSE keepalive**~~ | ✅ Done — `functools.partial(EventSourceResponse, ping=30)` injected before `sse_app()`. Sends SSE comment pings every 30s to reset router idle timers. |
| 4 | ~~**PR #3**~~ (`docker/build-push-action 5→7`) | ✅ Merged. |
