import { type FormEvent, type KeyboardEvent, useEffect, useRef, useState } from "react";
import type { DocumentInfo, Domain, SearchFilters } from "../api/types";
import { DOMAIN_LABELS } from "../lib/format";
import { FilterIcon, SendIcon } from "./icons";

const MAX_QUESTION_CHARS = 2000; // backend limit (MAX_QUESTION_CHARS)

interface Props {
  disabled: boolean;
  filters: SearchFilters;
  documents: DocumentInfo[];
  onFiltersChange: (filters: SearchFilters) => void;
  onSubmit: (question: string) => void;
}

export function Composer({ disabled, filters, documents, onFiltersChange, onSubmit }: Props) {
  const [text, setText] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = input.current; // grow with the text, up to the CSS max-height
    if (el) {
      el.style.height = "auto";
      el.style.height = `${el.scrollHeight}px`;
    }
  }, [text]);

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const question = text.trim();
    if (!question || disabled) return;
    onSubmit(question);
    setText("");
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) submit(event);
  };

  return (
    <form className="composer" onSubmit={submit}>
      <FilterBar filters={filters} documents={documents} onChange={onFiltersChange} />
      <div className="composer-box">
        <textarea
          ref={input}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Ask about pipeline regulations, PHMSA guidance or OT security…"
          rows={1}
          maxLength={MAX_QUESTION_CHARS}
          aria-label="Your question"
        />
        <button
          type="submit"
          className="button button-primary send"
          disabled={disabled || !text.trim()}
          aria-label="Send question"
        >
          <SendIcon />
        </button>
      </div>
      <p className="composer-hint muted small">Enter to send · Shift+Enter for a new line</p>
    </form>
  );
}

interface FilterBarProps {
  filters: SearchFilters;
  documents: DocumentInfo[];
  onChange: (filters: SearchFilters) => void;
}

function FilterBar({ filters, documents, onChange }: FilterBarProps) {
  const [open, setOpen] = useState(false);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !panel.current?.contains(e.target as Node) && setOpen(false);
    const escape = (e: globalThis.KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  const toggle = <T,>(values: T[], value: T) =>
    values.includes(value) ? values.filter((v) => v !== value) : [...values, value];
  const domains = Object.keys(DOMAIN_LABELS) as Domain[];
  const active = filters.domains.length + filters.document_ids.length;

  return (
    <div className="filters" role="group" aria-label="Limit the search to">
      {domains.map((domain) => (
        <button
          key={domain}
          type="button"
          className={`chip ${filters.domains.includes(domain) ? "is-on" : ""}`}
          aria-pressed={filters.domains.includes(domain)}
          onClick={() => onChange({ ...filters, domains: toggle(filters.domains, domain) })}
        >
          {DOMAIN_LABELS[domain]}
        </button>
      ))}
      <div className="filter-menu" ref={panel}>
        <button
          type="button"
          className={`chip ${filters.document_ids.length ? "is-on" : ""}`}
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
        >
          <FilterIcon width={14} height={14} />
          Documents{filters.document_ids.length ? ` (${filters.document_ids.length})` : ""}
        </button>
        {open && (
          <div className="filter-popover" role="dialog" aria-label="Choose documents">
            {documents.length === 0 && <p className="muted small">No documents available.</p>}
            {documents.map((d) => (
              <label key={d.id} className="filter-option">
                <input
                  type="checkbox"
                  checked={filters.document_ids.includes(d.id)}
                  onChange={() => onChange({ ...filters, document_ids: toggle(filters.document_ids, d.id) })}
                />
                <span>
                  <span className="filter-option-title">{d.short_title}</span>
                  <span className="muted small">{d.title}</span>
                </span>
              </label>
            ))}
          </div>
        )}
      </div>
      {active > 0 && (
        <button
          type="button"
          className="link-button small"
          onClick={() => onChange({ ...filters, domains: [], document_ids: [] })}
        >
          Clear filters
        </button>
      )}
    </div>
  );
}
