"""Typed contracts between ingestion stages.

    SourceSpec (registry) -> parser -> ParsedDocument (Sections of Paragraphs) -> chunker -> Chunk
"""

from __future__ import annotations

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
    options: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = None


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
    kind: str
    heading: str
    path: list[str]  # breadcrumb from the document root down to this section, inclusive
    citation: str
    paragraphs: list[Paragraph]
    source_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def pages(self) -> list[int]:
        return [p.page for p in self.paragraphs if p.page is not None]


class ParsedDocument(BaseModel):
    source_id: str
    parser: str
    parser_version: str
    sections: list[Section]
    # Lookup tables that are useful at query time but should not be retrieved as chunks (e.g. acronyms).
    dictionaries: dict[str, dict[str, str]] = Field(default_factory=dict)
    # Keys the parser expected to find (from a TOC or the source structure) - used for coverage checks.
    expected_keys: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Chunk(BaseModel):
    id: str
    document_id: str
    ordinal: int
    kind: str
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
