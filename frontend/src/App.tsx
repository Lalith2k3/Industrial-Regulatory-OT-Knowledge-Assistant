import { useCallback, useEffect, useState } from "react";
import { api } from "./api/client";
import { type Citation, type Conversation, type DocumentInfo, EMPTY_FILTERS, type Health } from "./api/types";
import { ChatView, PENDING_ID, type Selection } from "./components/ChatView";
import { Composer } from "./components/Composer";
import { EmptyState } from "./components/EmptyState";
import { Header } from "./components/Header";
import { AlertIcon } from "./components/icons";
import { Sidebar } from "./components/Sidebar";
import { SourcesPanel } from "./components/SourcesPanel";
import { useConversation } from "./hooks/useConversation";
import { useHashConversation } from "./hooks/useHashConversation";
import { useTheme } from "./hooks/useTheme";

export function App() {
  const [theme, toggleTheme] = useTheme();
  const [activeId, setActiveId] = useHashConversation();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [documents, setDocuments] = useState<DocumentInfo[]>([]);
  const [health, setHealth] = useState<Health | null>(null);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [sourcesOpen, setSourcesOpen] = useState(false);

  const refreshConversations = useCallback(() => {
    api
      .conversations()
      .then(setConversations)
      .catch(() => undefined); // the backend banner already reports an unreachable API
  }, []);

  useEffect(() => {
    Promise.all([api.health(), api.sources()])
      .then(([h, docs]) => {
        setHealth(h);
        setDocuments(docs);
      })
      .catch((error: Error) => setBackendError(error.message));
    refreshConversations();
  }, [refreshConversations]);

  const chat = useConversation(activeId, setActiveId, refreshConversations);

  useEffect(() => setSelection(null), [activeId]);
  useEffect(() => {
    // The streamed answer became a stored message: drop a selection that pointed at the stream.
    if (!chat.pending) setSelection((s) => (s?.messageId === PENDING_ID ? null : s));
  }, [chat.pending]);

  const select = (next: Selection) => {
    setSelection(next);
    setSourcesOpen(true);
  };
  const ask = (question: string) => {
    setSelection(null);
    void chat.ask(question, filters);
  };
  const newConversation = () => {
    setActiveId(null);
    setSidebarOpen(false);
  };
  const openConversation = (id: string) => {
    setActiveId(id);
    setSidebarOpen(false);
  };
  const deleteConversation = async (id: string) => {
    if (!window.confirm("Delete this conversation? This cannot be undone.")) return;
    try {
      await api.deleteConversation(id);
    } catch (error) {
      window.alert(`The conversation could not be deleted: ${(error as Error).message}`);
      return;
    }
    if (id === activeId) setActiveId(null);
    refreshConversations();
  };

  const shown = shownSources(selection, chat);
  const isEmpty = !activeId && chat.messages.length === 0 && !chat.pending;

  return (
    <div className="app">
      <Header
        health={health}
        theme={theme}
        onToggleTheme={toggleTheme}
        onToggleSidebar={() => setSidebarOpen((o) => !o)}
        onToggleSources={() => setSourcesOpen((o) => !o)}
      />
      {backendError && (
        <div className="banner banner-danger" role="alert">
          <AlertIcon /> Cannot reach the backend ({backendError}). Start it with <code>python -m app.api</code> in{" "}
          <code>backend/</code>.
        </div>
      )}
      {health?.offline_answers && (
        <div className="banner banner-warning">
          Offline mode: answers list the most relevant passages instead of being written by a language model. Set{" "}
          <code>RAG_ANTHROPIC_API_KEY</code> for written answers.
        </div>
      )}
      <div className="layout">
        <Sidebar
          conversations={conversations}
          activeId={activeId}
          disabled={chat.busy}
          open={sidebarOpen}
          onSelect={openConversation}
          onNew={newConversation}
          onDelete={(id) => void deleteConversation(id)}
          onClose={() => setSidebarOpen(false)}
        />
        <main className="main">
          <div className="main-scroll">
            {isEmpty ? (
              <EmptyState documents={documents} onExample={ask} />
            ) : chat.loadError ? (
              <div className="callout callout-danger load-error" role="alert">
                <AlertIcon />
                <div>
                  This conversation could not be opened: {chat.loadError}
                  <div className="callout-actions">
                    <button className="button button-small" onClick={newConversation}>
                      Start a new conversation
                    </button>
                  </div>
                </div>
              </div>
            ) : chat.loading ? (
              <p className="muted loading">Loading conversation…</p>
            ) : (
              <ChatView
                messages={chat.messages}
                pending={chat.pending}
                selection={selection}
                onSelect={select}
                onRetry={ask}
                onDismiss={chat.dismissPending}
              />
            )}
          </div>
          <Composer
            disabled={chat.busy || Boolean(backendError)}
            filters={filters}
            documents={documents}
            onFiltersChange={setFilters}
            onSubmit={ask}
          />
        </main>
        <SourcesPanel
          citations={shown.citations}
          selected={shown.selected}
          streaming={shown.streaming}
          open={sourcesOpen}
          onSelect={(number) => shown.messageId && setSelection({ messageId: shown.messageId, number })}
          onClose={() => setSourcesOpen(false)}
        />
        {(sidebarOpen || sourcesOpen) && (
          <div
            className="scrim"
            onClick={() => {
              setSidebarOpen(false);
              setSourcesOpen(false);
            }}
          />
        )}
      </div>
    </div>
  );
}

interface ShownSources {
  messageId: string | null;
  citations: Citation[];
  selected: number | null;
  streaming: boolean;
}

/** The sources panel follows the selected answer, else the streaming answer, else the latest answer. */
function shownSources(selection: Selection | null, chat: ReturnType<typeof useConversation>): ShownSources {
  const streamingSources = chat.pending && !chat.pending.error ? chat.pending.sources : null;
  if (selection?.messageId === PENDING_ID && streamingSources) {
    return { messageId: PENDING_ID, citations: streamingSources, selected: selection.number, streaming: true };
  }
  const selected = selection && chat.messages.find((m) => m.id === selection.messageId);
  if (selected) {
    return { messageId: selected.id, citations: selected.citations, selected: selection.number, streaming: false };
  }
  if (streamingSources?.length) {
    return { messageId: PENDING_ID, citations: streamingSources, selected: null, streaming: true };
  }
  const latest = [...chat.messages].reverse().find((m) => m.role === "assistant" && m.citations.length > 0);
  return { messageId: latest?.id ?? null, citations: latest?.citations ?? [], selected: null, streaming: false };
}
