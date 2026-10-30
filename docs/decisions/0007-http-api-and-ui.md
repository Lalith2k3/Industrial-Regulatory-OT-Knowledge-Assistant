# ADR-0007: HTTP API, server-side conversations and the React UI

- Status: Accepted
- Date: 2026-10-29

## Context

The query pipeline (ADR-0006) was reachable only from the command line. The UI needs an HTTP API,
follow-up questions need stored conversations (FR-08), and answers from Claude Opus take several
seconds, so the UI should show them as they are written.

## Decision

### API (`backend/app/api/`, FastAPI)

| Method and path | Purpose |
|---|---|
| `GET /api/health` | Status and models; `offline_answers` tells the UI it is in offline mode |
| `GET /api/sources` | Ingested documents, for the source filter (FR-07) |
| `GET /api/conversations` · `POST` · `GET /{id}` · `DELETE /{id}` | Conversation list, create, read, delete |
| `POST /api/conversations/{id}/messages` | Ask a question; the answer streams back as server-sent events |

Stream events: `stage` (rewriting, retrieving, generating) · `sources` (the numbered passages the
answer may cite) · `delta` (answer text) · `restart` (a fallback model starts the answer again) ·
`done` (the stored question and answer) · `error` (a controlled failure). Requests that cannot
succeed (unknown conversation, invalid question or filter) are rejected with 404 or 422 before the
stream starts.

The API layer holds no RAG logic (NFR-06). Routes call a `ConversationService`, which loads the
stored turns as history, calls the `RagService` and stores both the question and the answer,
including a failed answer, so that a reload shows what happened. Failed exchanges are left out of
the history sent with later questions. The synchronous service runs on a worker thread and reports
through an `AnswerListener` (`on_stage`, `on_sources`, `on_text`, `on_restart`); the generator only
sees the narrower `TextListener`.

### Streaming the structured answer

The answer remains structured output (`{"answer", "sufficient_evidence"}`, ADR-0006). The generator
always streams. `JsonStringFieldStream` decodes the `answer` value from the partial JSON as it
arrives, holding back escape sequences (and surrogate pairs) that are split across chunks. On a
mid-stream refusal the API inserts a `fallback` content block and the fallback model writes the
answer from the start, so the stream restarts and the final text is taken only from content after
the last fallback block.

### Conversations

The tables are `conversations` (id, title from the first question, created_at, updated_at) and
`conversation_messages` (role, content, status, citations as shown, `query_id` linking to
`query_logs`, standalone question). Conversations are not tied to users; there is no
authentication yet.

### UI (`frontend/`, React 19 + Vite + TypeScript)

- Three columns: conversations, chat, and a sources panel. Citation chips (`[n]`) in an answer
  select and expand the passage, with a link to the original (PDFs open at the page). Below
  1180 px the sources panel becomes a slide-over; below 760 px the conversation list does too.
- Light and dark themes defined as CSS tokens (steel blue for actions, amber for citations and
  caution). The theme is applied before first paint, and key text pairs measure 4.9:1 or higher
  in both themes.
- Answers render as Markdown without raw HTML. Answers flagged as insufficient evidence show a
  caution callout, and failed answers show the error with "Ask again".
- The open conversation is kept in the URL (`#/c/<id>`).
- In development Vite proxies `/api` to the API. After `npm run build` the API also serves the UI,
  so one process runs everything.

### Offline answers

`RAG_ANSWER_MODEL=local-extractive` replaces Claude with an extractive generator: it quotes the
opening sentences of the top three passages with their citations. Like the `local-hash` embedder,
it is for development only; it lets the full flow run without an Anthropic key, and the UI shows a
banner while it is active.

## Consequences

- Closing the browser does not cancel an answer: the worker finishes and stores it.
- No authentication or per-user conversations. Add both before the app is exposed beyond a
  trusted network.
- The Docker image serves the UI only if `frontend/dist` is built and mounted. There is no Node
  build stage yet.
