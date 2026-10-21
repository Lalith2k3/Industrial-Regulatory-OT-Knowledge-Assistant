# Data model

Implemented in `backend/app/ingestion/store.py` (knowledge base). Conversation and Message are
query-side tables, still to be built.

## documents
One row per approved source (`id` = registry id).

| Column | Notes |
|---|---|
| id, title, short_title, publisher, domain, doc_type, version, effective_date, source_url | From `data/sources.yaml` |
| local_path, content_sha256 | Source file and its hash (change detection) |
| parser, parser_version, chunker_version, embedding_model | Reproducibility (NFR-09) |
| chunk_count, metadata (jsonb), ingested_at | metadata holds quality stats, dictionaries (NIST acronyms), registry hash |

## chunks
One row per retrievable passage.

| Column | Notes |
|---|---|
| id | `sha256(source id, section key, part)[:24]`, stable across runs |
| document_id → documents.id | cascade delete |
| kind | section, definition, appendix, faq, guidance, interpretation, advisory, control, glossary_term, intro |
| domain, publisher, doc_type | Denormalised filters (FR-07) |
| section_key, citation, heading, section_path[] | `citation` is what the answer shows, e.g. `49 CFR 195.452(c)–(f)` |
| text, embed_text, token_count | `embed_text` = contextual header + text |
| page_start, page_end, page_label_start, page_label_end, source_url | Open the source at the right place (FR-05) |
| cfr_refs[], control_refs[] | Cross-references (GIN indexed) |
| metadata (jsonb) | e.g. faq_number, topic, interpretation_id, control_id, term, amendments |
| content_sha256, embedding_model, embedding vector(1024) | HNSW cosine index |
| tsv | Generated full-text vector (GIN) for hybrid search |

## ingestion_runs
id, started_at, finished_at, status (running, succeeded, partial, failed, aborted), options, per-document report, error.

## Conversation, Message (planned)
Conversation (id, created_at, filters) and Message (id, conversation_id, role, content,
retrieved chunk ids with scores, latency metrics) to support FR-08, FR-10 and NFR-07.
