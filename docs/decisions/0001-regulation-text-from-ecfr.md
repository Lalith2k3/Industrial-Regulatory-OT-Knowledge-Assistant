# ADR-0001: Use current eCFR XML for 49 CFR Parts 192 and 195

- Status: Accepted
- Date: 2026-10-07

## Context

The original corpus contained PHMSA's 2015 PDF compilations of Parts 192 and 195 (through
Amdt. 192-120 and 195-100). Those rules have been amended many times since. The PDFs are also
hard to parse: two columns, running headers, hyphenated line breaks, and amendment highlighting
whose meaning (deletion vs. addition) cannot be recovered reliably from the PDF.

## Decision

- Ingest Parts 192 and 195 from the eCFR Versioner API as XML, pinned to a point-in-time date in
  `data/sources.yaml` (`fetch.date`). The XML is committed so the knowledge base can be rebuilt
  exactly (NFR-09).
- Keep the 2015 PDFs on disk, registered with `enabled: false` and `superseded_by`, so they are
  never retrievable.
- Index the PHMSA O&M Enforcement Guidance without its "Existing Code Language" field, which
  quotes 2017 regulation text that would compete with the current text in retrieval.
- Do not ingest drafts (e.g. NIST SP 800-82r4 initial public draft); only final publications are approved.

## Consequences

- Regulation chunks cite sections and paragraph ranges (`49 CFR 195.452(c)–(f)`) and link to the
  pinned eCFR page; they have no PDF page numbers, which is correct for a codified regulation.
- Updating is a deliberate act: `python -m app.ingestion check-updates` reports newer eCFR text;
  bump `fetch.date`, run `fetch`, then `run`.
- The eCFR API requires response compression and blocks some automated clients; the fetcher
  sends a descriptive User-Agent and retries transient errors.
