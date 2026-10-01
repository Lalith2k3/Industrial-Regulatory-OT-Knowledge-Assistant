NFR-01 — Accuracy
Answers should be supported by retrieved source content.

NFR-02 — Traceability
Every answer containing factual claims should provide supporting citations.

NFR-03 — Latency
Target end-to-end response time should normally be under about 5 seconds.

NFR-04 — Reliability
Failures in document parsing, retrieval, or LLM generation should produce controlled errors.

NFR-05 — Security
API keys and secrets shall not be stored in source control.

NFR-06 — Maintainability
Document ingestion, retrieval, generation, API, and UI logic shall be separated into maintainable components.

NFR-07 — Observability
The system shall record retrieval latency, LLM latency, selected sources, and errors.

NFR-08 — Portability
The backend shall be containerized.

NFR-09 — Reproducibility
A developer should be able to rebuild the knowledge base from the source documents.