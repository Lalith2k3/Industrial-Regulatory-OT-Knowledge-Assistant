// Answers cite passages as "[2]" or "[1, 3]". Before rendering the Markdown, each number becomes a
// link to "#cite-n", which the answer renderer turns into a clickable citation chip.

const MARKER = /\[(\d+(?:\s*,\s*\d+)*)\](?!\()/g;
const CITE_HREF = /^#cite-(\d+)$/;

export function linkCitations(text: string): string {
  return text.replace(MARKER, (_match, group: string) =>
    group
      .split(",")
      .map((n) => `[${n.trim()}](#cite-${n.trim()})`)
      .join(""),
  );
}

export function citationNumber(href: string | undefined): number | null {
  const match = CITE_HREF.exec(href ?? "");
  return match ? Number(match[1]) : null;
}
