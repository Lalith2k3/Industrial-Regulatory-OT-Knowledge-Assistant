from abc import ABC, abstractmethod
from collections.abc import Iterable
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError

from app.ingestion.domain.errors import ParseError
from app.ingestion.domain.interfaces import Parser
from app.ingestion.domain.models import ParsedDocument, SourceSpec


class ParserOptions(BaseModel):
    """Per-source parser settings from `options:` in sources.yaml. Unknown keys are rejected,
    so a typo fails at startup instead of being silently ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class BaseParser(ABC):
    name: ClassVar[str]
    version: ClassVar[str]
    Options: ClassVar[type[ParserOptions]] = ParserOptions

    def validate_options(self, source: SourceSpec) -> None:
        self._options(source)

    def _options(self, source: SourceSpec) -> Any:
        try:
            return self.Options.model_validate(source.options)
        except ValidationError as exc:
            raise ParseError(f"{source.id}: invalid options for parser {self.name!r}: {exc}") from exc

    @abstractmethod
    def parse(self, source: SourceSpec, path: Path) -> ParsedDocument: ...


class ParserRegistry:
    """Looks parsers up by the name used in sources.yaml. New document families register a new
    parser here (in bootstrap.py); nothing else in the pipeline changes."""

    def __init__(self, parsers: Iterable[Parser]):
        self._parsers: dict[str, Parser] = {}
        for parser in parsers:
            if parser.name in self._parsers:
                raise ValueError(f"duplicate parser name {parser.name!r}")
            self._parsers[parser.name] = parser

    def get(self, name: str) -> Parser:
        try:
            return self._parsers[name]
        except KeyError:
            raise ParseError(f"unknown parser {name!r}; available: {sorted(self._parsers)}") from None

    def validate(self, source: SourceSpec) -> None:
        self.get(source.parser).validate_options(source)
