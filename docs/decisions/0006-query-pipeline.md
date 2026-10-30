# ADR-0006: Query pipeline - retrieval signals, grounded generation, query log

- Status: Accepted
- Date: 2026-10-28

## Context

The knowledge base (ADR-0002, ADR-0003) is built. The query side must answer questions only from
it, with citations (FR-02 to FR-06), filter by source category (FR-07), handle follow-ups (FR-08)
and log retrieval (FR-10, NFR-07, US-08). ADR-0003 left the fusion weights to be tuned against
`data/evaluation/questions.json`.

## Decision

### Structure

`backend/app/query/` follows the layering of ADR-0005:

```
domain/      models (QueryRequest, SearchQuery, RetrievedChunk, Answer, QueryLogEntry...), interfaces, errors
embedding/   Voyage query embedder (input_type="query"), offline hash embedder
retrieval/   QueryAnalyzer, Postgres candidate sources, reciprocal rank fusion, HybridRetriever
context.py   token-budgeted, numbered context sources
citations.py [n] markers -> citations with page and link
generation/  prompts, Claude answer generator and query rewriter, rewriters that need no model
storage/     query_logs table and repository
service.py   Searcher (embed -> analyse -> retrieve) and RagService (orchestration only)
evaluation.py retrieval evaluation against the gold set
bootstrap.py composition root;  cli.py: init-db, search, ask, evaluate-retrieval
```

The service depends only on the interfaces in `domain/`. A retrieval signal is a `CandidateSource`;
adding one (a reranker's candidates, another index) is a new class and one line in `bootstrap.py`.
The query side reads the knowledge base schema and reuses the reference extractors, rate limiter
and embedding cache from `app/ingestion`; ingestion never imports the query side.

### Retrieval

Three candidate sources, fused with weighted reciprocal rank fusion (k = 60, all weights 1.0):

| Signal | Fires when | Finds |
|---|---|---|
| vector | always | cosine similarity of the question embedding (HNSW, iterative scan under filters) |
| reference | the question names a CFR section or NIST control | chunks that *are* the section (by section key) first, then chunks that cite it |
| identifier | the question names a document id (PI-09-0005, ADB-11-04, AC-17) | full-text phrase match on the `tsv` column |

Measured on the 41 answerable gold questions (`python -m app.query evaluate-retrieval`, top 10)
and six identifier probes:

| Configuration | R@1 | R@3 | R@5 | R@10 | MRR | Identifier probes at rank 1 |
|---|---|---|---|---|---|---|
| vector + full-text on all question words + reference | 0.55 | 0.80 | 0.84 | 0.93 | 0.73 | - |
| vector only | 0.83 | 0.99 | 1.00 | 1.00 | 0.94 | 3 of 6 |
| **vector + reference + identifier (chosen)** | 0.83 | 0.99 | 1.00 | 1.00 | 0.93 | 6 of 6 |

Full-text search over all question words (OR-ed terms, ranked by cover density) was dropped: on
natural-language questions it ranks long, term-dense sections above the governing one, and no
weight helped (MRR 0.88 at weight 0.1). Requiring all words instead misses "Summarize advisory
bulletin ADB-11-04". Matching only identifier tokens keeps exact-match recall without the noise.
The evaluation reuses cached query embeddings, so re-tuning costs no API calls.

### Generation

- Claude Opus 5.5 (`claude-opus-5-5`, ADR-0003) answers from numbered `<source>` passages, citing
  them as `[n]`. Output is structured (`{"answer", "sufficient_evidence"}`), so FR-06 does not
  depend on parsing prose; `[n]` markers in the answer are resolved to citations, and numbers that
  match no source are dropped and logged.
- Effort defaults to `medium` (`RAG_ANSWER_EFFORT`). Raise it for harder questions, lower it if
  latency (NFR-03) matters more.
- Server-side refusal fallback (`fallbacks: "default"`) is on: NIST OT-security passages can trip
  the cyber safety classifier, and the API then re-runs the request on Anthropic's recommended
  fallback model. The served model is recorded in the query log.
- If no passage survives retrieval and filters, the service answers "not in the knowledge base"
  without calling the model.
- Follow-ups are rewritten into standalone questions for retrieval by Claude Haiku 5.5
  (`claude-haiku-5-5`, the current Haiku generation; ADR-0003 named Haiku 4.5). If rewriting fails,
  or no Anthropic key is configured, the previous user question is prefixed instead. The answer
  model itself sees the original question and the recent conversation.

### Logging

Every question writes one `query_logs` row: question, standalone question, filters, status, answer,
every retrieved chunk with its fused score, per-signal ranks and whether it reached the context,
cited chunk ids, per-stage latency, models and token usage, and the error for failed queries. A
failing log write never fails the answer.

## Consequences

- Conversations are stateless for now: the caller sends the history with each question. The
  Conversation/Message tables in the data model are still to be built, with the HTTP API.
- On the free Voyage tier (3 requests per minute), each new question costs one embedding request,
  so interactive use is limited to about three questions per minute. Repeated questions come from
  the on-disk query embedding cache.
- The gold set has few identifier-style questions; the identifier probes are a stopgap, and such
  questions should be added to `data/evaluation/questions.json`.
- Answer quality (faithfulness, key facts) is not evaluated yet; only retrieval is.
