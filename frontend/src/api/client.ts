import { readSse } from "./sse";
import type {
  AnswerEvent,
  Conversation,
  ConversationDetail,
  DocumentInfo,
  Health,
  SearchFilters,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) throw await toError(response);
  return (response.status === 204 ? undefined : await response.json()) as T;
}

async function toError(response: Response): Promise<ApiError> {
  let detail = response.statusText || `HTTP ${response.status}`;
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") detail = body.detail;
    else if (Array.isArray(body?.detail)) detail = body.detail.map((d: { msg: string }) => d.msg).join("; ");
  } catch {
    // not JSON: keep the status text
  }
  return new ApiError(detail, response.status);
}

export const api = {
  health: () => request<Health>("/api/health"),
  sources: () => request<{ documents: DocumentInfo[] }>("/api/sources").then((r) => r.documents),
  conversations: () => request<{ conversations: Conversation[] }>("/api/conversations").then((r) => r.conversations),
  conversation: (id: string) => request<ConversationDetail>(`/api/conversations/${encodeURIComponent(id)}`),
  createConversation: () => request<Conversation>("/api/conversations", { method: "POST" }),
  deleteConversation: (id: string) =>
    request<void>(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE" }),

  /** Ask a question; yields the answer's server-sent events until `done` or `error`. */
  async *ask(conversationId: string, question: string, filters: SearchFilters): AsyncGenerator<AnswerEvent> {
    const response = await fetch(`/api/conversations/${encodeURIComponent(conversationId)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ question, filters }),
    });
    if (!response.ok || !response.body) throw await toError(response);
    for await (const message of readSse(response.body)) {
      yield { event: message.event, data: JSON.parse(message.data) } as AnswerEvent;
    }
  },
};
