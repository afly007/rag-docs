.PHONY: up down restart logs build ingest ingest-force ingest-web backfill-tiers watch watch-stop stats pre-commit-install help

COMPOSE         = docker compose
INGEST_COMPOSE  = docker compose --profile ingest
SERVER_IP      ?= 192.168.0.50

## Start Qdrant and MCP server
up:
	$(COMPOSE) up -d

## Stop all containers
down:
	$(COMPOSE) down

## Restart MCP server (e.g. after code change)
restart:
	$(COMPOSE) build mcp-server && $(COMPOSE) up -d mcp-server

## Tail MCP server logs
logs:
	$(COMPOSE) logs -f mcp-server

## Build all images
build:
	$(COMPOSE) build mcp-server ingest

## Generate draft JSON sidecars for PDFs (review before ingesting)
gen-sidecars:
	$(INGEST_COMPOSE) run --rm --entrypoint python ingest gen_sidecar.py $(ARGS)

## Ingest new PDFs in ./docs (skips already-ingested)
ingest:
	$(INGEST_COMPOSE) run --rm ingest $(ARGS)

## Re-ingest all PDFs (overwrites existing chunks)
ingest-force:
	$(INGEST_COMPOSE) run --rm ingest --force $(ARGS)

## Ingest web pages from a JSON manifest file into community tier (trust_tier=4)
## Usage: make ingest-web ARGS="/docs/community.json"
ingest-web:
	$(INGEST_COMPOSE) run --rm --entrypoint python ingest ingest_web.py $(ARGS)

## Backfill trust_tier=1 / source_type=vendor-doc on existing chunks missing these fields (run once after upgrading)
backfill-tiers:
	$(INGEST_COMPOSE) run --rm --entrypoint python ingest backfill_tiers.py

## Start continuous watch mode — auto-ingests new PDFs dropped into ./docs/
watch:
	docker compose --profile watch up -d ingest-watch

## Stop the watch container
watch-stop:
	docker compose --profile watch stop ingest-watch

## Open stats page in browser (macOS)
stats:
	open http://$(SERVER_IP):8000/stats

## Install pre-commit hooks into local git (requires pipx: sudo apt install pipx)
pre-commit-install:
	pipx install pre-commit && pre-commit install

## Show available commands
help:
	@grep -E '^##' Makefile | sed 's/## //'
