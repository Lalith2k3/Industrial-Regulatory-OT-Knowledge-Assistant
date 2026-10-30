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
  decisions/    architecture decision records (ADR-0001 to ADR-0007)
backend/
  app/core/           settings, logging
  app/ingestion/      ingestion pipeline (see ADR-0005 for the layering)
    domain/           models, interfaces, errors
    sources/          source registry, eCFR client
    parsing/          one parser per document family
    chunking/         structure-aware chunker and policies
    embedding/        Voyage embedder, rate limiter, cache
    storage/          PostgreSQL schema and repositories, artifacts
    pipeline.py       orchestration
    bootstrap.py      wiring (composition root)
  app/query/          question answering (see ADR-0006)
    domain/           models, interfaces, errors
    embedding/        Voyage query embedder
    retrieval/        query analysis, vector / reference / identifier search, rank fusion
    generation/       prompts, Claude answer generator and follow-up rewriter
    storage/          query log, conversations, document catalogue
    service.py        orchestration;  conversations.py: server-side conversations
    bootstrap.py      wiring (composition root)
  app/api/            FastAPI: REST + streamed answers (server-sent events), serves the built UI
  tests/              unit/, corpus/ (real documents), integration/ (PostgreSQL)
frontend/       React + Vite + TypeScript UI (chat, sources panel, light/dark theme)
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
uv run pytest                                  # all tests (db tests need RAG_TEST_DATABASE_URL)
uv run pytest -m "not corpus and not db"       # fast unit tests only
uv run ruff check . && uv run ruff format --check .   # lint + formatting
uv run mypy                                    # strict type checking
```

Always run `uv run …` from `backend/`, where the project's environment lives.

## Running the app

Needs an ingested knowledge base. For written answers set `RAG_ANTHROPIC_API_KEY` in `.env`; without
one, `RAG_ANSWER_MODEL=local-extractive` runs everything offline (answers list the top passages).

```bash
# terminal 1: the API on http://127.0.0.1:8000
cd backend && uv run python -m app.api
# terminal 2: the UI with hot reload on http://localhost:5173 (proxies /api to port 8000)
cd frontend && npm install && npm run dev
```

Single process instead: `cd frontend && npm run build`, then `python -m app.api` serves the UI at
http://127.0.0.1:8000. Frontend checks: `npm run typecheck`, `npm test`.

## Asking questions from the command line

Needs an ingested knowledge base, plus `RAG_ANTHROPIC_API_KEY` in `.env` for answers.

```bash
uv run python -m app.query search "How often must a crude oil right-of-way be patrolled?"  # retrieval only
uv run python -m app.query ask "How often must a crude oil right-of-way be patrolled?"
uv run python -m app.query ask "What does SI-2 add for OT?" --domain ot_security
uv run python -m app.query evaluate-retrieval                  # recall@k / MRR on the gold set
```

See [docs/architecture/query-flow.md](docs/architecture/query-flow.md).

No API key yet? `RAG_EMBEDDING_MODEL=local-hash` runs the whole pipeline offline with non-semantic
vectors; the next run with Voyage re-embeds automatically. See
[docs/architecture/ingestion-flow.md](docs/architecture/ingestion-flow.md).

## Documentation

- [Project scope](docs/requirements/project-scope.md)
- [Functional requirements](docs/requirements/functional-requirements.md)
- [Non-functional requirements](docs/requirements/nonfunctional-requirements.md)
- [User stories](docs/requirements/user-stories.md)
- [Ingestion flow](docs/architecture/ingestion-flow.md) · [Query flow](docs/architecture/query-flow.md) · [Data model](docs/architecture/data-model.md)
- [ADR-0006: query pipeline](docs/decisions/0006-query-pipeline.md) · [ADR-0007: HTTP API and UI](docs/decisions/0007-http-api-and-ui.md)

## Status

Ingestion, query pipeline, HTTP API with streamed answers, server-side conversations and the React
UI are implemented and tested. Pending: authentication, answer-quality evaluation, and a container
build stage for the UI.
