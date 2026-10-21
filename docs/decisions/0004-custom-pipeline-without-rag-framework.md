# ADR-0004: Plain Python pipeline instead of a RAG framework

- Status: Accepted
- Date: 2026-10-07

## Context

LangChain and LlamaIndex offer loaders, splitters and vector-store adapters. Their generic PDF
loaders work page by page and their splitters are size-based, which is exactly what this corpus
must avoid (ADR-0002). Their abstractions would also hide the parts we most need to control and
debug: section boundaries, citation strings, page labels and metadata.

## Decision

Build ingestion as a small typed pipeline: Pydantic models between stages
(`SourceSpec → ParsedDocument → Chunk`), PyMuPDF and lxml for parsing, SQLAlchemy Core for
storage, the official `voyageai` SDK for embeddings, and an argparse CLI. Intermediate output is
written to `data/processed/<source>/` (`sections.json`, `chunks.jsonl`, `quality.json`) for
debugging (US-08).

## Consequences

- More code to own, but every stage is explicit and unit-tested, and there is no framework upgrade churn.
- Nothing prevents using a framework on the query side later if it earns its place.
