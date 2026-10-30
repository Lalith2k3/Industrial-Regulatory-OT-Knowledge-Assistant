import type { Citation, DocType, Domain } from "../api/types";

export const DOMAIN_LABELS: Record<Domain, string> = {
  pipeline_safety: "Pipeline safety",
  ot_security: "OT security",
};

export const DOC_TYPE_LABELS: Record<DocType, string> = {
  regulation: "Regulation",
  enforcement_guidance: "Enforcement guidance",
  faq: "FAQ",
  standard: "Standard",
};

const KIND_LABELS: Record<string, string> = {
  section: "Section",
  definition: "Definition",
  appendix: "Appendix",
  faq: "FAQ",
  guidance: "Guidance",
  interpretation: "Interpretation",
  advisory: "Advisory bulletin",
  control: "Control",
  glossary_term: "Glossary",
  intro: "Introduction",
};

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

/** "p. 54" or "pp. 54–55" from printed page labels, or null for sources without pages. */
export function pageLabel(citation: Pick<Citation, "page_label_start" | "page_label_end">): string | null {
  const { page_label_start: start, page_label_end: end } = citation;
  if (!start) return null;
  return end && end !== start ? `pp. ${start}–${end}` : `p. ${start}`;
}

export function relativeTime(iso: string, now: Date = new Date()): string {
  const seconds = Math.round((now.getTime() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days} d ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
