import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import type { AnswerStage, Citation, ConversationMessage, SearchFilters } from "../api/types";

/** The answer being streamed, before the server confirms it with the stored messages. */
export interface PendingAnswer {
  question: string;
  stage: AnswerStage | null;
  text: string;
  sources: Citation[]; // every passage the answer may cite, numbered as in the text
  restarted: boolean; // a fallback model started the answer again
  error: string | null; // the request failed before the server could store anything
}

export interface ConversationState {
  messages: ConversationMessage[];
  pending: PendingAnswer | null;
  loading: boolean;
  loadError: string | null;
  busy: boolean;
  ask: (question: string, filters: SearchFilters) => Promise<void>;
  dismissPending: () => void;
}

export function useConversation(
  activeId: string | null,
  setActiveId: (id: string) => void,
  onChanged: () => void,
): ConversationState {
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [pending, setPending] = useState<PendingAnswer | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const createdHere = useRef<string | null>(null); // created by ask(): it has no stored messages to load

  useEffect(() => {
    setLoadError(null);
    if (activeId === null) {
      setMessages([]);
      return;
    }
    if (createdHere.current === activeId) {
      createdHere.current = null;
      return;
    }
    let cancelled = false;
    setLoading(true);
    setMessages([]);
    api
      .conversation(activeId)
      .then((detail) => !cancelled && setMessages(detail.messages))
      .catch((error: unknown) => !cancelled && setLoadError(message(error)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [activeId]);

  const update = (change: Partial<PendingAnswer> | ((p: PendingAnswer) => Partial<PendingAnswer>)) =>
    setPending((p) => (p ? { ...p, ...(typeof change === "function" ? change(p) : change) } : p));

  const ask = useCallback(
    async (question: string, filters: SearchFilters) => {
      setPending({ question, stage: null, text: "", sources: [], restarted: false, error: null });
      let id = activeId;
      try {
        if (!id) {
          id = (await api.createConversation()).id;
          createdHere.current = id;
          setActiveId(id);
        }
        for await (const ev of api.ask(id, question, filters)) {
          switch (ev.event) {
            case "stage":
              update({ stage: ev.data.stage });
              break;
            case "sources":
              update({ sources: ev.data.citations });
              break;
            case "delta":
              update((p) => ({ text: p.text + ev.data.text }));
              break;
            case "restart":
              update({ text: "", restarted: true });
              break;
            case "done":
              setMessages((m) => [...m, ev.data.question, ev.data.answer]);
              setPending(null);
              break;
            case "error": {
              // The server stored the question and a failed answer; show them as stored.
              const detail = await api.conversation(id);
              setMessages(detail.messages);
              setPending(null);
              break;
            }
          }
        }
      } catch (error) {
        update({ error: message(error) });
      } finally {
        onChanged();
      }
    },
    [activeId, setActiveId, onChanged],
  );

  const dismissPending = useCallback(() => setPending(null), []);

  return {
    messages,
    pending,
    loading,
    loadError,
    busy: pending !== null && pending.error === null,
    ask,
    dismissPending,
  };
}

function message(error: unknown): string {
  if (error instanceof Error) return error.message;
  return "Something went wrong";
}
