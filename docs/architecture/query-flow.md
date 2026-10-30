# Query flow

```
 React UI ──POST /api/conversations/{id}/messages──► FastAPI (backend/app/api/)
    ▲                                                   │
    │ server-sent events: stage · sources · delta ·     ▼
    │ restart · done | error                         ConversationService: stored turns become the history,
    │                                                   question and answer are stored afterwards (ADR-0007)
    └───────────────────────────────────────────────────┤
          ▼
 QueryRequest (question, earlier turns, source filters)          backend/app/query/
          │
          ├── follow-up? ──► QueryRewriter (Claude Haiku 5.5; previous-question fallback)
          ▼                     standalone question for retrieval
 Searcher
   QueryEmbedder             voyage-4-large, 1024-d, input_type=query, rate-limited, cached
   QueryAnalyzer             CFR sections (195.452), NIST controls (AC-17), document ids (PI-09-0005)
   HybridRetriever           candidate sources, each filtered by domain / publisher / doc type / document
     vector                  cosine similarity (HNSW)
     reference               chunks that are / cite the named section or control
     identifier              full-text phrase match on named document ids
     └─ reciprocal rank fusion ─► top 10 RetrievedChunks (score + per-signal ranks)
          ▼
 ContextBuilder              best first, within 6,000 tokens, numbered [1]..[n]
          │   (nothing retrieved: "not in the knowledge base", no model call)
          ▼
 AnswerGenerator             Claude Opus 5.5, structured output {answer, sufficient_evidence},
                             cites [n], server-side refusal fallback
          ▼
 CitationResolver            [n] ─► citation, document, version, pages, link (#page=N for PDFs)
          ▼
 Answer (status: answered | insufficient_evidence, text, citations, timings)
          │
          └──────────────► query_logs (retrieved chunks, ranks, cited ids, latency, models, usage, errors)
```

See ADR-0006 for the retrieval measurements behind the choice of signals.

## Commands (run from `backend/`)

| Command | Purpose |
|---|---|
| `uv run python -m app.query init-db` | Create the `query_logs` table (also created on first `ask`) |
| `uv run python -m app.query search "question" [--domain D] [--document ID]` | Show what retrieval returns and why (no LLM) |
| `uv run python -m app.query ask "question" [filters] [--json]` | Answer with citations |
| `uv run python -m app.query evaluate-retrieval [--show-hits]` | Recall@k and MRR against `data/evaluation/questions.json` |

Filters: `--domain pipeline_safety|ot_security`, `--doc-type regulation|enforcement_guidance|faq|standard`,
`--publisher PHMSA|NIST`, `--document <source id>`; each may be repeated.

## Investigating a poor answer (US-08)

1. `search` the same question: are the right passages retrieved, and by which signal?
2. Look up the question in `query_logs`: the standalone question, what reached the context
   (`in_context`), what was cited, and the error if it failed.
3. If the passage itself is wrong, check `data/processed/<source>/chunks.jsonl` (ingestion).
