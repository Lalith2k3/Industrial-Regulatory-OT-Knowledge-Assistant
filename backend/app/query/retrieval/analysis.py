import re

from app.ingestion.references import ReferenceExtractor
from app.query.domain.models import SearchFilters, SearchQuery

# Letter prefix, hyphen, number groups: interpretation ids (PI-09-0005), advisory bulletins
# (ADB-11-04, also printed "ADB -99-03"), control ids (AC-17). Embeddings match these poorly.
_IDENTIFIER = re.compile(r"\b([A-Za-z]{1,6})\s?-\s?(\d{1,4}(?:-\d{1,4})*)\b")


class QueryAnalyzer:
    """Builds the SearchQuery every candidate source receives.

    Uses the same reference extractors as the chunker, so "195.452" or "AC-17" in a question
    matches exactly what ingestion stored in chunks.cfr_refs / control_refs and section keys.
    """

    def __init__(self, cfr_refs: ReferenceExtractor, control_refs: ReferenceExtractor):
        self._cfr_refs = cfr_refs
        self._control_refs = control_refs

    def analyze(self, text: str, embedding: list[float], filters: SearchFilters) -> SearchQuery:
        return SearchQuery(
            text=text,
            embedding=embedding,
            cfr_refs=self._cfr_refs.extract(text),
            control_refs=self._control_refs.extract(text.upper()),  # users type "ac-17" too
            identifiers=list(dict.fromkeys(f"{m[1].upper()}-{m[2]}" for m in _IDENTIFIER.finditer(text))),
            filters=filters,
        )
