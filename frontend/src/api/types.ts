// Mirrors the backend's JSON (backend/app/query/domain/models.py, backend/app/api/schemas.py).

export type Domain = "pipeline_safety" | "ot_security";
export type DocType = "regulation" | "enforcement_guidance" | "faq" | "standard";
export type AnswerStatus = "answered" | "insufficient_evidence" | "failed";
export type AnswerStage = "rewriting" | "retrieving" | "generating";
export type Role = "user" | "assistant";

export interface SearchFilters {
  domains: Domain[];
  publishers: string[];
  doc_types: DocType[];
  document_ids: string[];
}

export interface Citation {
  number: number;
  chunk_id: string;
  document_id: string;
  document_title: string;
  document_version: string;
  citation: string;
  heading: string;
  kind: string;
  page_start: number | null;
  page_end: number | null;
  page_label_start: string | null;
  page_label_end: string | null;
  url: string | null;
  text: string;
}

export interface Conversation {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConversationMessage {
  id: string;
  conversation_id: string;
  role: Role;
  content: string;
  created_at: string;
  status: AnswerStatus | null;
  citations: Citation[];
  query_id: string | null;
  standalone_question: string | null;
}

export interface ConversationDetail {
  conversation: Conversation;
  messages: ConversationMessage[];
}

export interface DocumentInfo {
  id: string;
  title: string;
  short_title: string;
  publisher: string;
  domain: Domain;
  doc_type: DocType;
  version: string;
  effective_date: string | null;
  source_url: string | null;
  chunk_count: number;
}

export interface Health {
  status: string;
  answer_model: string;
  embedding_model: string;
  offline_answers: boolean;
}

/** Server-sent events of POST /api/conversations/{id}/messages (backend/app/api/streaming.py). */
export type AnswerEvent =
  | { event: "stage"; data: { stage: AnswerStage } }
  | { event: "sources"; data: { citations: Citation[] } }
  | { event: "delta"; data: { text: string } }
  | { event: "restart"; data: Record<string, never> }
  | { event: "done"; data: { question: ConversationMessage; answer: ConversationMessage } }
  | { event: "error"; data: { message: string } };

export const EMPTY_FILTERS: SearchFilters = { domains: [], publishers: [], doc_types: [], document_ids: [] };
