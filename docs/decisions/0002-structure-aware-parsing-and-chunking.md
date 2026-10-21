# ADR-0002: Parsers per document type and structure-aware chunking

- Status: Accepted
- Date: 2026-10-07

## Context

Every document in the corpus has a strong, citable structure: CFR sections and paragraphs, numbered
FAQs, enforcement-guidance forms keyed by code section, and a NIST guide with 277 bookmarks and a
control overlay. Answers must cite the authoritative passage (FR-04, US-02). Fixed-size or
page-based chunking would cut across these units, mix unrelated sections, and lose the citation.

A single generic parser cannot handle all document layouts. Supporting a small set of known document
families well is more valuable than supporting arbitrary documents poorly.

## Decision

- **Parsers by document family**, selected per source in the registry:
  `ecfr_xml`, `enforcement_guidance_pdf`, `faq_pdf`, `nist_sp_pdf`. Each produces `Section`s,
  the smallest unit worth citing (a CFR section, a definition, one FAQ, one interpretation, one
  security control). A shared PyMuPDF layout layer removes running headers and footers by position,
  keeps printed page labels (PDF page ≠ printed page), extracts tables, and repairs hyphenation
  using the document's own vocabulary.
- **Chunking keeps a section whole when it fits** (regulations ≤ 800 estimated tokens) and
  otherwise packs paragraphs, preferring breaks at top-level paragraphs `(a)`, `(b)`. Oversized
  tables are split by rows with the header repeated. Overlap (60 tokens) is used only for NIST
  prose and never crosses a section boundary.
- **Contextual header for embedding**: each chunk embeds as
  `<document> \n <section path> \n\n <text>`; the UI and LLM get the text without the header.
- **Cross-references as metadata**: `cfr_refs` (e.g. `195.505`) and `control_refs` (e.g. `AC-17`)
  are extracted per chunk, so guidance and FAQs can be joined to the regulation they interpret.
- **Non-knowledge content is excluded**: amendment tables, tables of contents, NIST References and
  Change Log. The NIST acronym list becomes a lookup dictionary for query expansion instead of chunks.
- **Quality gates** (coverage of expected units ≥ 90 %, no empty or duplicate chunks, page numbers
  present for PDFs) block a document from being written; the previous version stays live.

## Consequences

- Adding a document of a known family is a registry entry. A new family needs a new parser, by
  design; the `Parser` protocol and `pdf_layout` helpers keep that small.
- Parser and chunker versions are stored per document; bumping either re-processes affected documents.
