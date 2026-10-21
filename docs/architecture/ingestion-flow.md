# Ingestion flow

```
 data/sources.yaml (approved registry: id, parser, domain, doc_type, version, fetch date)
          │
          ├── fetch ──────────► eCFR Versioner API ──► data/ecfr/title-49-part-19x.xml (pinned date)
          ▼
 Parser (per document family)                    backend/app/ingestion/parsers/
   ecfr_xml                 section / definition / appendix (legal structure from XML)
   enforcement_guidance_pdf form fields, interpretations, advisory bulletins per code section
   faq_pdf                  one Q&A per FAQ, topic, cited sections, footnotes
   nist_sp_pdf              bookmark sections, one section per control, glossary terms, acronym table
          │   PDFs share pdf_layout: header/footer removal, printed page labels, tables,
          │   paragraph grouping, vocabulary-based de-hyphenation
          ▼
 ParsedDocument (Sections of Paragraphs, expected units, dictionaries)
          ▼
 Chunker                                         chunking.py
   keep section whole if it fits · else pack paragraphs, break at (a)/(b) · split tables by rows
   contextual header for embedding · cfr_refs / control_refs · deterministic ids · content hash
          ▼
 Quality gates                                   quality.py
   coverage of expected units ≥ 90 % · no empty/duplicate chunks · PDF chunks have pages
          │   (failure: document skipped, previous version stays live)
          ├──────────────► data/processed/<source>/ sections.json · chunks.jsonl · quality.json
          ▼
 Embedder (voyage-4-large, 1024-d, input_type=document; only new/changed chunks)
          ▼
 PostgreSQL + pgvector (one transaction per document)
   documents · chunks (embedding HNSW + tsv GIN + filter columns) · ingestion_runs
```

## Commands (run from `backend/`)

| Command | Purpose |
|---|---|
| `uv run python -m app.ingestion fetch` | Download the pinned eCFR snapshots |
| `uv run python -m app.ingestion check-updates` | Report whether eCFR has newer text than the pinned date |
| `uv run python -m app.ingestion init-db` | Create the pgvector extension and tables |
| `uv run python -m app.ingestion run --dry-run` | Parse, chunk and run quality gates without embeddings or DB |
| `uv run python -m app.ingestion run [--source ID] [--force]` | Ingest; unchanged documents are skipped |

A run is idempotent. A document is re-processed when its file hash, registry entry, parser version,
chunker version or embedding model changes. Documents removed from the registry, or with
`enabled: false`, are pruned from the knowledge base (disable with `--no-prune`).

## Adding a document (US-07)

1. Put the file under `data/` (or add an eCFR `fetch:` entry).
2. Add a registry entry in `data/sources.yaml` with one of the existing parsers.
3. `uv run python -m app.ingestion run --source <id> --dry-run`, check `data/processed/<id>/quality.json`.
4. `uv run python -m app.ingestion run --source <id>`.

A document from a new family (a different layout) needs a new parser; see ADR-0002.
