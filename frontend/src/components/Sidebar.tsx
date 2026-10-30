import type { Conversation } from "../api/types";
import { relativeTime } from "../lib/format";
import { CloseIcon, PlusIcon, TrashIcon } from "./icons";

interface Props {
  conversations: Conversation[];
  activeId: string | null;
  disabled: boolean; // while an answer streams
  open: boolean; // narrow screens: slide-over state
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onClose: () => void;
}

export function Sidebar({ conversations, activeId, disabled, open, onSelect, onNew, onDelete, onClose }: Props) {
  return (
    <aside className={`sidebar ${open ? "is-open" : ""}`} aria-label="Conversations">
      <div className="sidebar-top">
        <button className="button button-primary new-chat" onClick={onNew} disabled={disabled}>
          <PlusIcon /> New conversation
        </button>
        <button className="icon-button only-narrow" onClick={onClose} aria-label="Close conversations">
          <CloseIcon />
        </button>
      </div>
      <div className="sidebar-label">Recent</div>
      <nav className="conversation-list">
        {conversations.length === 0 && <p className="muted small sidebar-empty">No conversations yet.</p>}
        {conversations.map((c) => (
          <div key={c.id} className={`conversation-item ${c.id === activeId ? "is-active" : ""}`}>
            <button
              className="conversation-open"
              onClick={() => onSelect(c.id)}
              disabled={disabled && c.id !== activeId}
              aria-current={c.id === activeId ? "page" : undefined}
            >
              <span className="conversation-title">{c.title ?? "New conversation"}</span>
              <span className="conversation-time">{relativeTime(c.updated_at)}</span>
            </button>
            <button
              className="icon-button conversation-delete"
              onClick={() => onDelete(c.id)}
              disabled={disabled}
              aria-label={`Delete conversation: ${c.title ?? "New conversation"}`}
            >
              <TrashIcon width={16} height={16} />
            </button>
          </div>
        ))}
      </nav>
      <p className="sidebar-footer muted small">
        Answers come only from the approved knowledge base. Verify against the cited source before acting.
      </p>
    </aside>
  );
}
