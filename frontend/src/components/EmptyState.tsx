import type { DocumentInfo, Domain } from "../api/types";
import { DOC_TYPE_LABELS, DOMAIN_LABELS } from "../lib/format";

const EXAMPLES = [
  "How often must we patrol the right-of-way on a crude oil pipeline?",
  "What test pressure and hold time does Part 195 require for a hydrostatic test?",
  "Can unqualified helpers perform a covered task under a qualified individual's direction?",
  "What does NIST SP 800-82 recommend when OT components can't be patched?",
];

interface Props {
  documents: DocumentInfo[];
  onExample: (question: string) => void;
}

export function EmptyState({ documents, onExample }: Props) {
  const byDomain = new Map<Domain, DocumentInfo[]>();
  for (const d of documents) byDomain.set(d.domain, [...(byDomain.get(d.domain) ?? []), d]);

  return (
    <div className="empty">
      <h1>Ask the approved knowledge base</h1>
      <p className="muted">
        Answers are written only from approved regulations and guidance, and every statement cites the passage it
        comes from. If the sources don't cover a question, the assistant says so.
      </p>

      <div className="examples">
        {EXAMPLES.map((q) => (
          <button key={q} className="example" onClick={() => onExample(q)}>
            {q}
          </button>
        ))}
      </div>

      {documents.length > 0 && (
        <section className="corpus" aria-label="Approved sources">
          <h2>Approved sources</h2>
          {[...byDomain.entries()].map(([domain, docs]) => (
            <div key={domain} className="corpus-group">
              <div className="corpus-domain">{DOMAIN_LABELS[domain]}</div>
              <ul>
                {docs.map((d) => (
                  <li key={d.id}>
                    <span className="corpus-title">{d.short_title}</span>
                    <span className="muted small">
                      {DOC_TYPE_LABELS[d.doc_type]} · {d.version}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </section>
      )}
    </div>
  );
}
