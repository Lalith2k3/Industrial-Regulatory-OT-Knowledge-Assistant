# RAG_PROJECT

An industrial regulatory and OT knowledge assistant that answers questions using only an
approved document knowledge base and returns supporting citations.

## Stack

React UI → FastAPI backend → RAG service → vector store + document corpus.
See [docs/architecture/high-level-architecture.md](docs/architecture/high-level-architecture.md).

## Layout

```
data/
  sources.yaml  approved-source registry (what gets ingested, and how)
  ecfr/         49 CFR Parts 192 and 195, current eCFR XML (pinned snapshot)
  phmsa/        PHMSA guidance PDFs (+ superseded 2015 CFR compilations, not ingested)
  nist/         NIST SP 800-82r3 OT security guide (PDF)
  evaluation/   gold evaluation question set
  processed/    ingestion artifacts per source (generated, git-ignored)
docs/
  requirements/ scope, functional and non-functional requirements, user stories
  architecture/ high-level architecture, ingestion flow, query flow, data model
  decisions/    architecture decision records (ADR-0001 to ADR-0004: ingestion)
backend/        Python backend; app/ingestion is implemented, API and retrieval pending
frontend/       React UI (not yet implemented)
```

## Ingestion quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and Docker.

```bash
cp .env.example .env              # add RAG_VOYAGE_API_KEY
docker compose up -d db           # PostgreSQL 17 + pgvector on localhost:5433
cd backend
uv sync
uv run python -m app.ingestion run --dry-run   # parse + chunk + quality gates only
uv run python -m app.ingestion run             # embed and load the knowledge base
uv run pytest                                  # unit + corpus tests
RAG_TEST_DATABASE_URL=postgresql+psycopg://rag:rag@localhost:5433/rag uv run pytest -m db
```

No API key yet? `RAG_EMBEDDING_MODEL=local-hash` runs the whole pipeline offline with non-semantic
vectors; the next run with Voyage re-embeds automatically. See
[docs/architecture/ingestion-flow.md](docs/architecture/ingestion-flow.md).

## Documentation

- [Project scope](docs/requirements/project-scope.md)
- [Functional requirements](docs/requirements/functional-requirements.md)
- [Non-functional requirements](docs/requirements/nonfunctional-requirements.md)
- [User stories](docs/requirements/user-stories.md)
- [Ingestion flow](docs/architecture/ingestion-flow.md) · [Query flow](docs/architecture/query-flow.md) · [Data model](docs/architecture/data-model.md)

## Status

Ingestion pipeline implemented and tested. Retrieval, generation, API and UI pending.
