# Data model

Knowledge base: `backend/app/ingestion/storage/schema.py`. Query log and conversations:
`backend/app/query/storage/schema.py`.

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

## query_logs
One row per question (FR-10, NFR-07, US-08).

| Column | Notes |
|---|---|
| id, created_at | `id` is the `query_id` returned with the answer |
| question, standalone_question, history_turns, filters (jsonb) | What was asked; the rewritten question used for retrieval |
| status, answer, error | answered, insufficient_evidence or failed |
| retrieved (jsonb) | Every retrieved chunk: chunk_id, citation, fused score, per-signal ranks, in_context |
| cited_chunk_ids[] | Chunks the answer cites |
| timings, models, usage (jsonb) | Milliseconds per stage; embedding / rewrite / generation models; token usage |

## conversations
id, title (the first question), created_at, updated_at (FR-08).

## conversation_messages
| Column | Notes |
|---|---|
| id, conversation_id → conversations.id | cascade delete |
| role, content, created_at | user question or assistant answer; a failed answer's content is the error shown to the user |
| status | assistant only: answered, insufficient_evidence, failed |
| citations (jsonb) | the citations as shown with the answer |
| query_id, standalone_question | link to `query_logs` (US-08); the rewritten question used for retrieval |
