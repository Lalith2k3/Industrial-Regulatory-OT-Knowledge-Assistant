# ADR-0003: Voyage embeddings and a PostgreSQL + pgvector hybrid index

- Status: Accepted
- Date: 2026-10-07

## Context

Queries mix natural language ("how often must I patrol a transmission line?") with exact
identifiers ("195.452", "PI-74-0122", "AC-17", "MAOP") that dense embeddings match poorly.
The corpus is small (about 1,700 chunks, about 335k tokens), so embedding cost is negligible and quality
matters more. The architecture already names PostgreSQL as the knowledge store.

## Decision

- **Embeddings**: Voyage AI `voyage-4-large` at 1024 dimensions, `input_type="document"` for
  chunks and `"query"` for questions. Recommended by Anthropic for Claude-based RAG; 32k context
  is far above our chunk size. The `Embedder` protocol keeps the provider replaceable; a
  `local-hash` embedder exists only for offline development and tests.
- **Store**: PostgreSQL 17 + pgvector. Each chunk row holds the embedding (HNSW, cosine) and a
  generated `tsv` column (GIN) so retrieval can fuse vector and full-text results. Filter
  columns (`domain`, `publisher`, `doc_type`) are denormalised onto chunks for FR-07, with GIN
  indexes on `cfr_refs` and `control_refs`.
- **Answer generation** (query side, later): Claude Opus 5.5 (`claude-opus-5-5`) for grounded
  answers with citations; Claude Haiku 4.5 (`claude-haiku-4-5`) for cheap query rewriting of follow-ups.

## Free-tier operation (2026-10-17)

The project runs on Voyage's free allowance (first 200M tokens of `voyage-4-large` free per
account) without a payment method, which limits the account to 3 requests and 10,000 tokens
per minute. The embedder therefore:

- sizes each batch to fit one minute of token budget, learning the ratio between our token
  estimate and Voyage's own count from each response;
- paces requests with a 60-second sliding window, and on a 429 waits a full window and retries;
- appends every finished batch to `data/processed/.embedding-cache/<model>-<dim>.jsonl`, so an
  interrupted run resumes without re-embedding (and without spending tokens twice).

A full build of the corpus (about 0.4M tokens) takes roughly 45–60 minutes once; later runs only
embed changed chunks. Adding a payment method later costs nothing within the free tokens and
raises the limits; then set `RAG_EMBEDDING_REQUESTS_PER_MINUTE` / `RAG_EMBEDDING_TOKENS_PER_MINUTE`.

## Consequences

- Changing the embedding dimension requires rebuilding the `chunks` table; `init-db` refuses a
  mismatch instead of corrupting data. Changing the model re-embeds automatically on the next run.
- Re-ingestion only embeds chunks whose content hash changed.
- A reranker and the fusion weights are retrieval-side decisions, to be tuned against
  `data/evaluation/questions.json`.
