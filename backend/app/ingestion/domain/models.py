"""Typed contracts between ingestion stages.

SourceSpec (registry) -> Parser -> ParsedDocument (Sections of Paragraphs) -> Chunker -> Chunk
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class DocType(StrEnum):
    REGULATION = "regulation"
    ENFORCEMENT_GUIDANCE = "enforcement_guidance"
    FAQ = "faq"
    STANDARD = "standard"


class Domain(StrEnum):
    PIPELINE_SAFETY = "pipeline_safety"
    OT_SECURITY = "ot_security"


class SectionKind(StrEnum):
    SECTION = "section"
    DEFINITION = "definition"
    APPENDIX = "appendix"
    FAQ = "faq"
    GUIDANCE = "guidance"
    INTERPRETATION = "interpretation"
    ADVISORY = "advisory"
    CONTROL = "control"
    GLOSSARY_TERM = "glossary_term"
    INTRO = "intro"


class DocumentStatus(StrEnum):
    INGESTED = "ingested"
    UNCHANGED = "unchanged"
    CHECKED = "checked"  # dry run: parsed, chunked and quality-checked only
    FAILED = "failed"
    PRUNED = "pruned"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    ABORTED = "aborted"


# -- registry --------------------------------------------------------------------------------


class EcfrFetchSpec(BaseModel):
    title: int
    part: int
    date: date  # point-in-time snapshot; pinned so the knowledge base is reproducible


class SourceSpec(BaseModel):
    """One approved document, as declared in data/sources.yaml."""

    id: str
    title: str
    short_title: str  # leads every citation, e.g. "49 CFR Part 195"
    publisher: str
    domain: Domain
    doc_type: DocType
    parser: str
    path: Path  # relative to the data directory
    version: str
    effective_date: date | None = None
    source_url: str | None = None
    enabled: bool = True
    superseded_by: str | None = None
    fetch: EcfrFetchSpec | None = None
    options: dict[str, Any] = Field(default_factory=dict)  # validated by the parser's own Options model
    notes: str | None = None


# -- parser output ---------------------------------------------------------------------------


class Paragraph(BaseModel):
    text: str
    page: int | None = None  # 1-based PDF page index where the paragraph starts
    page_label: str | None = None  # page number as printed on that page
    page_end: int | None = None  # set when the paragraph continues onto later pages
    page_label_end: str | None = None
    is_table: bool = False
    # Top-level paragraph designation in effect, e.g. "a" for 49 CFR 195.452(a)(2)(i).
    top_marker: str | None = None
    # True when this paragraph opens a new top-level unit; the chunker prefers to split here.
    starts_unit: bool = False


class Section(BaseModel):
    """The smallest addressable unit a parser recognises: a CFR section, one FAQ, one control..."""

    key: str  # stable within the document; drives deterministic chunk ids
    kind: SectionKind
    heading: str
    path: list[str]  # breadcrumb from the document root down to this section, inclusive
    citation: str
    paragraphs: list[Paragraph]
    source_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Cross-references the parser knows structurally (in addition to those found in the text).
    own_cfr_ref: str | None = None  # the CFR section this text *is*; never listed as a reference to itself
    cfr_refs: list[str] = Field(default_factory=list)  # e.g. the regulation an interpretation explains
    control_refs: list[str] = Field(default_factory=list)  # e.g. the NIST control this section specifies
    # Cite chunks of a split section by paragraph range: "49 CFR 195.452(c)–(f)".
    paragraph_citations: bool = False


class ParsedDocument(BaseModel):
    source_id: str
    parser: str
    parser_version: str
    sections: list[Section]
    paginated: bool = False  # PDF sources: every chunk must carry page numbers for citations
    # Lookup tables that are useful at query time but should not be retrieved as chunks (e.g. acronyms).
    dictionaries: dict[str, dict[str, str]] = Field(default_factory=dict)
    # Units the parser expected to find (from a TOC or the source structure) - used for coverage checks.
    expected_keys: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# -- chunker output --------------------------------------------------------------------------


class Chunk(BaseModel):
    id: str
    document_id: str
    ordinal: int
    kind: SectionKind
    section_key: str
    citation: str
    heading: str
    section_path: list[str]
    text: str  # what the UI shows and the LLM reads
    embed_text: str  # text + contextual header; what gets embedded
    token_count: int
    page_start: int | None = None
    page_end: int | None = None
    page_label_start: str | None = None
    page_label_end: str | None = None
    source_url: str | None = None
    cfr_refs: list[str] = Field(default_factory=list)
    control_refs: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    content_sha256: str  # hash of embed_text; unchanged chunks keep their embeddings on re-ingest


# -- persistence -----------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentFingerprint:
    """Everything that determines a document's stored content. Equal fingerprints: nothing to do."""

    content_sha256: str  # the source file
    spec_sha256: str  # its registry entry
    parser_version: str
    chunker_version: str
    embedding_model: str


@dataclass(frozen=True)
class StoredDocument:
    fingerprint: DocumentFingerprint
    chunk_count: int


@dataclass(frozen=True)
class DocumentRecord:
    """What gets written for one ingested document (the chunks travel separately)."""

    source: SourceSpec
    local_path: str
    parser: str
    fingerprint: DocumentFingerprint
    metadata: dict[str, Any]
