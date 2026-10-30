# ADR-0005: Layered package structure with dependency injection

- Status: Accepted
- Date: 2026-10-27

## Context

The first version of the ingestion code worked but mixed responsibilities: the pipeline function
hashed files, parsed, chunked, checked quality, wrote artifacts, embedded and stored in one block,
and constructed its own database and embedding clients. The storage class held the schema, the
document repository and the run log. Parsers read loosely-typed option dictionaries, and the chunker
knew which metadata keys each parser used. Values such as the CFR title ("49"), the eCFR site URL
and a default database password were written into the code.

## Decision

Organise `backend/app/ingestion/` by responsibility, with dependencies pointing inwards:

```
domain/      models, interfaces (Parser, Embedder, DocumentRepository, RunLog, ArtifactSink), errors
sources/     source registry (sources.yaml) and the eCFR API client
parsing/     one parser per document family + shared PDF layout helpers; typed, validated options
chunking/    Chunker and chunk policies
embedding/   Voyage embedder, rate limiter, on-disk cache, offline hash embedder
storage/     schema, PostgreSQL repositories, artifact writer
pipeline.py  orchestration only, against the domain interfaces
quality.py   quality gate
bootstrap.py composition root: reads Settings, builds concrete objects, wires them together
cli.py       command line
```

- **Single responsibility:** each class has one reason to change.
- **Open/closed:** a new document family is a new parser plus one line in `bootstrap.py`; the
  pipeline, chunker and storage do not change. Parsers declare structural references (own CFR
  section, linked regulation, control id) as typed `Section` fields, so the chunker needs no
  knowledge of any particular parser.
- **Interface segregation and dependency inversion:** the pipeline depends on small protocols
  and receives implementations from `bootstrap.py`. It can be unit-tested with in-memory fakes.
- **Configuration:** environment-specific values (paths, credentials, endpoints, provider limits,
  model names, CFR parts used for reference detection) live in `Settings`; there is no default
  database URL. Domain constants (chunk sizes, PDF layout thresholds) are named and documented next
  to the code that uses them, or exposed as typed per-source parser options.
- **Tooling:** `ruff` (lint + format) and `mypy --strict` run clean.

## Consequences

- Behaviour is unchanged. All 1,678 chunks are identical to the pre-refactor output (the only
  byte-level difference is a now-deterministic metadata key order), and an ingestion run against the
  existing database reports every document as unchanged, so no re-embedding was needed.
- Typos in `options:` in sources.yaml now fail at start-up instead of being silently ignored.
- Tests are split into `tests/unit` (fast, no I/O beyond fixtures), `tests/corpus` (real
  documents) and `tests/integration` (PostgreSQL).
