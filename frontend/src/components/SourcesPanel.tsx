import { useEffect, useRef } from "react";
import type { Citation } from "../api/types";
import { kindLabel, pageLabel } from "../lib/format";
import { CloseIcon, ExternalIcon, SourcesIcon } from "./icons";

interface Props {
  citations: Citation[];
  selected: number | null;
  streaming: boolean; // citations are the passages under review, not yet the cited ones
  open: boolean; // medium screens: slide-over state
  onSelect: (number: number) => void;
  onClose: () => void;
}

export function SourcesPanel({ citations, selected, streaming, open, onSelect, onClose }: Props) {
  const list = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (selected === null) return;
    list.current?.querySelector(`[data-number="${selected}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [selected, citations]);

  return (
    <aside className={`sources ${open ? "is-open" : ""}`} aria-label="Sources">
      <div className="sources-header">
        <h2>
          <SourcesIcon /> {streaming ? "Passages under review" : "Sources"}
        </h2>
        <button className="icon-button only-medium" onClick={onClose} aria-label="Close sources">
          <CloseIcon />
        </button>
      </div>
      <div className="sources-list" ref={list}>
        {citations.length === 0 ? (
          <p className="muted small sources-empty">
            Cited passages appear here. Select a citation number in an answer to read the exact text and open the
            original document.
          </p>
        ) : (
          citations.map((c) => (
            <SourceCard key={`${c.number}-${c.chunk_id}`} citation={c} selected={c.number === selected} onSelect={onSelect} />
          ))
        )}
      </div>
    </aside>
  );
}

function SourceCard({
  citation: c,
  selected,
  onSelect,
}: {
  citation: Citation;
  selected: boolean;
  onSelect: (number: number) => void;
}) {
  const pages = pageLabel(c);
  return (
    <article className={`source ${selected ? "is-selected" : ""}`} data-number={c.number}>
      <button className="source-head" onClick={() => onSelect(c.number)} aria-expanded={selected}>
        <span className="source-number">{c.number}</span>
        <span className="source-titles">
          <span className="source-citation">{c.citation}</span>
          <span className="muted small">
            {c.document_title} · {c.document_version}
            {pages ? ` · ${pages}` : ""}
          </span>
        </span>
        <span className="tag">{kindLabel(c.kind)}</span>
      </button>
      <blockquote className={`source-text ${selected ? "" : "is-clamped"}`}>{c.text}</blockquote>
      {c.url && (
        <a className="source-link" href={c.url} target="_blank" rel="noopener noreferrer">
          Open original <ExternalIcon width={14} height={14} />
        </a>
      )}
    </article>
  );
}
