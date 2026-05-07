.PHONY: up down restart logs build ingest ingest-force stats pre-commit-install help

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

## Open stats page in browser (macOS)
stats:
	open http://$(SERVER_IP):8000/stats

## Install pre-commit hooks into local git (requires pipx: sudo apt install pipx)
pre-commit-install:
	pipx install pre-commit && pre-commit install

## Show available commands
help:
	@grep -E '^##' Makefile | sed 's/## //'
