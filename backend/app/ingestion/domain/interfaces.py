"""Interfaces (ports) the pipeline depends on. Concrete implementations live in parsing/,
embedding/ and storage/ and are wired together in bootstrap.py, so the pipeline never imports
PyMuPDF, Voyage or SQLAlchemy and can be unit-tested with in-memory fakes."""

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from app.ingestion.domain.models import (
    Chunk,
    DocumentRecord,
    ParsedDocument,
    RunStatus,
    SourceSpec,
    StoredDocument,
)


class Parser(Protocol):
    """Turns one source file into addressable Sections.

    Bump `version` whenever output changes: documents whose stored parser version differs are
    re-processed, which keeps the knowledge base reproducible (NFR-09).
    """

    @property
    def name(self) -> str:
        """The `parser:` value used in sources.yaml."""

    @property
    def version(self) -> str: ...

    def validate_options(self, source: SourceSpec) -> None:
        """Raise ParseError if `source.options` are not valid for this parser."""

    def parse(self, source: SourceSpec, path: Path) -> ParsedDocument: ...


class Embedder(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...


class DocumentRepository(Protocol):
    def ensure_schema(self) -> None: ...

    def get(self, document_id: str) -> StoredDocument | None: ...

    def document_ids(self) -> list[str]: ...

    def reusable_embeddings(self, document_id: str, model: str) -> dict[str, tuple[str, list[float]]]:
        """chunk id -> (content hash, embedding) for chunks embedded with `model`."""

    def replace(self, record: DocumentRecord, chunks: Sequence[Chunk], embeddings: dict[str, list[float]]) -> None:
        """Atomically make `chunks` the document's content."""

    def delete(self, document_id: str) -> None: ...


class RunLog(Protocol):
    def start(self, options: dict[str, Any]) -> str: ...

    def finish(self, run_id: str, status: RunStatus, report: dict[str, Any], error: str | None = None) -> None: ...


class ArtifactSink(Protocol):
    """Receives intermediate output so a poor answer can be traced to parsing or chunking (US-08)."""

    def write(self, parsed: ParsedDocument, chunks: Sequence[Chunk], quality: dict[str, Any]) -> None: ...
