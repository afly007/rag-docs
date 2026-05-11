# Configuration Reference

Operational reference for deploying and configuring Distill. For a conceptual overview see [README.md](README.md); for day-to-day usage see [USAGE.md](USAGE.md).

---

## Environment variables

Copy `.env.example` to `.env` and edit before starting the stack.

### Core

| Variable | Default | Required | Description |
|---|---|---|---|
| `OPENAI_API_KEY` | — | Yes | OpenAI API key — used for embeddings (`text-embedding-3-small`) and auto-sidecar generation (`gpt-4o-mini`) |
| `COLLECTION_NAME` | `distill` | No | Qdrant collection name. Change to namespace multiple independent doc sets. |
| `IMAGE_BASE` | `ghcr.io/afly007/distill` | For pull | GHCR registry prefix. Must match `ghcr.io/<owner>/<repo>` exactly. Required for `docker compose pull`. |

### Re-ranking (optional)

Re-ranking fetches 20 candidates from Qdrant and uses a cross-encoder to select the top 5. Off by default.

| Variable | Default | Description |
|---|---|---|
| `RERANKER` | _(off)_ | `local` — flashrank CPU cross-encoder (~22 MB, downloaded once); `cohere` — Cohere Rerank API |
| `COHERE_API_KEY` | — | Required when `RERANKER=cohere` |

```bash
RERANKER=local
# or
RERANKER=cohere
COHERE_API_KEY=your-key
```

### Search behaviour

| Variable | Default | Description |
|---|---|---|
| `TIER_BOOST_4` | `0.75` | Score multiplier for community (tier-4) results in `search_docs()`. Values below `1.0` apply a penalty. Set to `1.0` to disable. |

### Browser clipper

| Variable | Default | Description |
|---|---|---|
| `CLIP_API_KEY` | — | Secret key for the `/clip` endpoint. Required to use the browser extension. Generate with `openssl rand -hex 32`. |

### Proxy security (optional, requires `COMPOSE_PROFILES=tls`)

| Variable | Default | Description |
|---|---|---|
| `ADMIN_USER` | — | Username for HTTP basic auth on `/stats` and `/files`. Must be set together with `ADMIN_PASSWORD_HASH`. |
| `ADMIN_PASSWORD_HASH` | — | bcrypt hash of the admin password. Generate with: `docker run --rm caddy:2-alpine caddy hash-password --plaintext yourpassword` |
| `CLIP_RATE_LIMIT` | `20` | Maximum `/clip` requests per IP per minute. Protects OpenAI API credits from bulk abuse. |

### TLS / Caddy proxy

Only read when `COMPOSE_PROFILES=tls`. See [TLS setup](#tls-setup) below.

| Variable | Default | Description |
|---|---|---|
| `COMPOSE_PROFILES` | _(none)_ | Set to `tls` to start the Caddy reverse proxy alongside the core stack. |
| `TLS_MODE` | `internal` | `internal` — Caddy's built-in CA; `dns` — Let's Encrypt via DNS-01 challenge |
| `TLS_DOMAIN` | `distill.local` | Hostname Caddy will serve. Must match what clients connect to. |
| `TLS_DNS_PROVIDER` | — | Required when `TLS_MODE=dns`. One of: `cloudflare`, `route53`, `acmedns`, `digitalocean` |
| `CF_API_TOKEN` | — | Cloudflare API token (Zone:DNS:Edit). Required when `TLS_DNS_PROVIDER=cloudflare`. |
| `AWS_REGION` | `us-east-1` | AWS region for Route53. Uses SDK credential chain — also accepts `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`. |
| `ACMEDNS_HOST` | — | ACME-DNS server URL. Required when `TLS_DNS_PROVIDER=acmedns`. |
| `DO_AUTH_TOKEN` | — | DigitalOcean API token. Required when `TLS_DNS_PROVIDER=digitalocean`. |

---

## Docker Compose profiles

The core stack (`qdrant` + `mcp-server`) starts without any profile. Additional services are gated behind profiles.

| Profile | Service | How to activate |
|---|---|---|
| `ingest` | One-shot ingestion run | `make ingest` or `docker compose --profile ingest run --rm ingest` |
| `watch` | Continuous watch — polls `./docs/` every 30s | `make watch` / `make watch-stop` |
| `tls` | Caddy TLS reverse proxy | `COMPOSE_PROFILES=tls` in `.env`, then `docker compose up -d` |

Multiple profiles can be active simultaneously:

```bash
COMPOSE_PROFILES=tls,watch
```

---

## TLS setup

Caddy runs as an optional reverse proxy on `:443`, forwarding to `mcp-server:8000` on the internal Docker network. Port `8000` remains available for direct HTTP access — no existing clients break when you enable TLS.

### Option A — Internal CA (`TLS_MODE=internal`)

Best for: LAN-only deployment, no public domain required.

Caddy generates its own root CA and issues a certificate for your chosen hostname. Each client needs to trust Caddy's root certificate once.

**1. Configure `.env`:**

```bash
COMPOSE_PROFILES=tls
TLS_MODE=internal
TLS_DOMAIN=distill.local   # or any hostname — must resolve to 192.168.0.50
```

**2. Add a DNS or hosts entry** on each client machine so the hostname resolves:

```
# /etc/hosts  (or Windows: C:\Windows\System32\drivers\etc\hosts)
192.168.0.50  distill.local
```

Or add an A record in your local DNS server / router.

**3. Start the stack:**

```bash
docker compose up -d
```

**4. Export and trust the root certificate:**

```bash
# Copy the cert from the container
docker compose cp caddy:/data/pki/authorities/local/root.crt ./caddy-root.crt
```

Then import `caddy-root.crt` into each client's trust store:

| OS | How to trust |
|---|---|
| **macOS** | `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain caddy-root.crt` |
| **Linux** | Copy to `/usr/local/share/ca-certificates/caddy-root.crt` then `sudo update-ca-certificates` |
| **Windows** | Double-click the file → Install Certificate → Local Machine → Trusted Root Certification Authorities |
| **Firefox** | Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import |

**5. Update MCP client URLs** — see [MCP client configuration](#mcp-client-configuration) below. Remove `--allow-http` from any `mcp-remote` configs.

---

### Option B — Let's Encrypt via DNS challenge (`TLS_MODE=dns`)

Best for: public trust (no cert import), works with private IPs as long as you control DNS.

Requires a real domain and API credentials for your DNS provider. Caddy handles certificate issuance and renewal automatically.

**1. Create a DNS record** pointing your chosen hostname at the server's IP:

```
distill.yourdomain.com   A   192.168.0.50
```

**2. Configure `.env` — Cloudflare example:**

```bash
COMPOSE_PROFILES=tls
TLS_MODE=dns
TLS_DOMAIN=distill.yourdomain.com
TLS_DNS_PROVIDER=cloudflare
CF_API_TOKEN=your-zone-dns-edit-token
```

Cloudflare token permissions: **Zone → DNS → Edit** scoped to the target zone. Generate at Cloudflare Dashboard → My Profile → API Tokens.

**2a. Route53 alternative:**

```bash
TLS_DNS_PROVIDER=route53
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=AKIAxxxxxxxxxxxxxxxx
AWS_SECRET_ACCESS_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

The IAM policy needs `route53:ChangeResourceRecordSets` and `route53:GetChange` / `route53:ListHostedZonesByName`.

**2b. DigitalOcean alternative:**

```bash
TLS_DNS_PROVIDER=digitalocean
DO_AUTH_TOKEN=your-personal-access-token
```

**2c. ACME-DNS alternative** (provider-agnostic, requires a self-hosted [acme-dns](https://github.com/joohoi/acme-dns) instance):

```bash
TLS_DNS_PROVIDER=acmedns
ACMEDNS_HOST=https://auth.acme-dns.io
```

**3. Start the stack:**

```bash
docker compose up -d
```

Caddy will obtain the certificate on first start. Check logs with `docker compose logs -f caddy`.

**4. Update MCP client URLs** — see below.

---

### Verifying TLS is working

```bash
curl -I https://distill.local/stats                    # internal CA (after trusting cert)
curl -I https://distill.yourdomain.com/stats           # DNS challenge
openssl s_client -connect distill.local:443 </dev/null # inspect certificate
```

---

## MCP client configuration

### HTTP (default, no TLS)

**Claude Code** (`~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "distill": {
      "type": "sse",
      "url": "http://192.168.0.50:8000/sse"
    }
  }
}
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`):

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

**Cursor / Windsurf** — point to `http://192.168.0.50:8000/sse` in the MCP settings panel.

---

### HTTPS (with TLS enabled)

Replace the IP/port with your TLS domain and remove `--allow-http`.

**Claude Code:**

```json
{
  "mcpServers": {
    "distill": {
      "type": "sse",
      "url": "https://distill.yourdomain.com/sse"
    }
  }
}
```

**Claude Desktop:**

```json
{
  "mcpServers": {
    "distill": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://distill.yourdomain.com/sse"]
    }
  }
}
```

> **Internal CA note:** if using `TLS_MODE=internal`, the client OS must trust Caddy's root certificate (step 4 of Option A above) before these configs will work.

---

## Proxy security hardening

These features are active whenever Caddy is running (`COMPOSE_PROFILES=tls`). No extra configuration is needed for security headers and rate limiting — they are always on. Admin auth is opt-in.

### Security response headers

Injected by Caddy on every response:

| Header | Value | Purpose |
|---|---|---|
| `Strict-Transport-Security` | `max-age=31536000` | Tells browsers to use HTTPS only for this domain for one year |
| `X-Content-Type-Options` | `nosniff` | Prevents MIME-type sniffing |
| `X-Frame-Options` | `DENY` | Blocks the UI from being embedded in an iframe |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Limits referrer leakage on cross-origin navigation |
| `Server` | _(removed)_ | Prevents Caddy from advertising itself |

### Admin authentication

Protects `/stats` and `/files` with HTTP basic auth. Off by default — set both env vars to enable.

**1. Generate a password hash:**

```bash
docker run --rm caddy:2-alpine caddy hash-password --plaintext yourpassword
```

Copy the output (a `$2a$...` bcrypt string).

**2. Add to `.env`:**

```bash
ADMIN_USER=admin
ADMIN_PASSWORD_HASH=JDJhJDE0JG...
```

**3. Restart Caddy:**

```bash
docker compose up -d caddy
```

> Both variables must be set together or both left unset. Setting only one causes Caddy to refuse to start.

### Rate limiting on `/clip`

The `/clip` endpoint fetches URLs and generates embeddings — bulk requests can exhaust OpenAI API credits. Caddy limits each client IP to `CLIP_RATE_LIMIT` requests per minute (default: 20). Excess requests receive HTTP 429.

```bash
CLIP_RATE_LIMIT=20   # default; lower to tighten
```

---

## File browser

The file browser at `http(s)://YOUR_SERVER:8000/files` lets you upload, manage, and annotate documents without SSH access.

| Feature | Description |
|---|---|
| Upload | Drag-and-drop or click to select `.pdf` or `.md` files. Files are ingested immediately using the same TOC-aware chunking pipeline as the CLI ingestor. |
| Auto-metadata (✦) | Available on PDF rows. Scans the first 10 pages with GPT-4o-mini and pre-fills the metadata modal for review. Does not write anything until you click Save. |
| Edit metadata (✎) | Opens the sidecar edit modal. Check **Re-ingest after saving** to immediately re-index the file with the new metadata. |
| Download (↓) | Downloads the original file. |
| Delete (✕) | Removes the file, its sidecar, and all associated Qdrant chunks. |

Columns are sortable — click any header to sort ascending, click again to reverse.

---

## Stats dashboard

`http(s)://YOUR_SERVER:8000/stats` — auto-refreshes every 60 seconds (every 3 seconds when an ingest is active).

| Section | Description |
|---|---|
| Summary cards | Collection name, document count, total chunks, queries today, total queries, average latency, average score |
| Document Catalog | All ingested documents with vendor, product, version, doc type, trust tier, pages, and chunk counts |
| Recent Queries | Last 30 queries — timestamp, text, active filters, score, result count, top source, latency |
| Coverage Gaps | Queries scoring below threshold — topics likely missing from your corpus |
| Most Referenced Sources | Which documents get retrieved most, with average relevance score |
| Slowest Queries | Top 10 by latency |

---

## Sidecar metadata format

A `.json` file with the same base name as the document (e.g. `ios-xe-17.json` next to `ios-xe-17.pdf`).

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

All fields are optional. Documents without sidecars are still indexed and searchable.

| Field | Values |
|---|---|
| `vendor` | Lowercase vendor name: `cisco`, `juniper`, `arista`, `hpe`, `hewlett-packard-enterprise`, `palo-alto`, `fortinet`, `nokia`, … |
| `product` | Lowercase product/OS: `ios-xe`, `junos`, `eos`, `aos-cx`, `pan-os`, `sr-os`, … |
| `version` | Version string: `17.9.1`, `23.2R1`, `4.28.0`, … |
| `doc_type` | `cli-reference`, `config-guide`, `design-guide`, `validated-design`, `release-notes`, `white-paper`, `datasheet` |
| `trust_tier` | `1` vendor doc, `2` validated design, `3` internal, `4` community (clip only) |
| `source_type` | `vendor-doc`, `validated-design`, `internal` |

**Vendor aliases:** searching `vendor=aruba` also returns documents tagged `hewlett-packard-enterprise`, `hpe`, and `arubanetworks` — the server normalises these automatically.

**Design guides without a version:** leave `version` and `product` blank — missing fields match any filter, not no filter.

After editing sidecars, re-ingest to apply the changes:

```bash
make ingest-force
# or a single file:
make ingest-force ARGS="/docs/ios-xe-17.pdf"
```

---

## Operational commands

```bash
# Stack
make up              # start Qdrant + mcp-server
make down            # stop everything
make restart         # rebuild + restart mcp-server
make logs            # tail mcp-server logs

# Ingestion
make ingest          # ingest new files (skips already-ingested)
make ingest-force    # re-ingest everything (use after editing sidecars)
make watch           # auto-ingest new files every 30s
make watch-stop      # stop the watch container

# Metadata
make gen-sidecars              # auto-generate .json sidecars via GPT-4o-mini
make gen-sidecars ARGS="--force"  # overwrite existing sidecars

# Maintenance
make stats           # open stats page in browser (macOS)
make build           # build both images locally
```

### Delete and recreate a collection

Required when changing the embedding model or vector configuration:

```bash
curl -X DELETE http://localhost:6333/collections/distill
make ingest-force
```

### Export the query log

```bash
docker run --rm -v distill_mcp_data:/data alpine \
  sh -c "apk add -q sqlite && sqlite3 /data/queries.db \
  'SELECT ts, query, top_score, top_source FROM queries ORDER BY id DESC LIMIT 50'"
```

### Rotate the Caddy root certificate

Caddy renews TLS certificates automatically. If you need to rotate the CA root (e.g. after a server rebuild):

```bash
docker compose stop caddy
docker volume rm distill_caddy_data
docker compose up -d caddy
docker compose cp caddy:/data/pki/authorities/local/root.crt ./caddy-root.crt
# Re-import caddy-root.crt on each client
```

---

## Upgrading

Pull the latest images and restart:

```bash
cd ~/distill
docker compose pull
docker compose up -d
```

Images are published to GHCR on every merge to `main`:

```
ghcr.io/afly007/distill/mcp-server:latest
ghcr.io/afly007/distill/ingest:latest
ghcr.io/afly007/distill/caddy:latest
```
