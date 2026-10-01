# RAG_PROJECT

An industrial regulatory and OT knowledge assistant that answers questions using only an
approved document knowledge base and returns supporting citations.

## Stack

React UI → FastAPI backend → RAG service → vector store + document corpus.
See [docs/architecture/high-level-architecture.md](docs/architecture/high-level-architecture.md).

## Layout

```
data/
  phmsa/        PHMSA pipeline safety regulations and guidance (PDF)
  nist/         NIST SP 800-82 OT security guidance (PDF)
  evaluation/   evaluation question set
docs/
  requirements/ scope, functional and non-functional requirements, user stories
  architecture/ high-level architecture, ingestion flow, query flow, data model
  decisions/    architecture decision records
backend/        FastAPI service (not yet implemented)
frontend/       React UI (not yet implemented)
```

## Documentation

- [Project scope](docs/requirements/project-scope.md)
- [Functional requirements](docs/requirements/functional-requirements.md)
- [Non-functional requirements](docs/requirements/nonfunctional-requirements.md)
- [User stories](docs/requirements/user-stories.md)
- [Ingestion flow](docs/architecture/ingestion-flow.md) · [Query flow](docs/architecture/query-flow.md) · [Data model](docs/architecture/data-model.md)

## Status

Requirements and architecture drafted. Backend and frontend implementation pending.
