from app.ingestion.errors import ParseError
from app.ingestion.parsers.base import Parser
from app.ingestion.parsers.ecfr import EcfrXmlParser
from app.ingestion.parsers.enforcement_guidance import EnforcementGuidanceParser
from app.ingestion.parsers.faq import FaqPdfParser
from app.ingestion.parsers.nist import NistSpParser

_PARSERS: dict[str, Parser] = {
    p.name: p
    for p in (EcfrXmlParser(), EnforcementGuidanceParser(), FaqPdfParser(), NistSpParser())
}


def get_parser(name: str) -> Parser:
    try:
        return _PARSERS[name]
    except KeyError:
        raise ParseError(f"unknown parser {name!r}; available: {sorted(_PARSERS)}") from None


__all__ = ["Parser", "get_parser"]
