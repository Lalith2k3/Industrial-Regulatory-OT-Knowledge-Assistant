                         ┌──────────────────────┐
                         │       React UI       │
                         │                      │
                         │ Chat + Sources       │
                         └──────────┬───────────┘
                                    │
                                   HTTP
                                    │
                         ┌──────────▼───────────┐
                         │       FastAPI        │
                         │      Backend API     │
                         └──────────┬───────────┘
                                    │
                         ┌──────────▼───────────┐
                         │     RAG Service      │
                         └──────────┬───────────┘
                                    │
                      ┌─────────────┼──────────────┐
                      │             │              │
                      ▼             ▼              ▼
                  Retriever       LLM Service    Logging
                      │
                      ▼
                 Knowledge DB
                      │
            chunks + metadata + embeddings