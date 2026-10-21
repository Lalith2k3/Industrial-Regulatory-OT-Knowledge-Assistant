from pathlib import Path
from typing import Protocol

from app.ingestion.models import ParsedDocument, SourceSpec


class Parser(Protocol):
    """A parser turns one source file into addressable Sections.

    Bump `version` whenever output changes: the pipeline re-processes documents whose stored
    parser version differs, which keeps the knowledge base reproducible (NFR-09).
    """

    name: str
    version: str

    def parse(self, source: SourceSpec, path: Path) -> ParsedDocument: ...
