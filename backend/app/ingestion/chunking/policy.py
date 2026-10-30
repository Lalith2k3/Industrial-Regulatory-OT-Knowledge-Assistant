from dataclasses import dataclass

from app.ingestion.domain.models import DocType


@dataclass(frozen=True)
class ChunkPolicy:
    target_tokens: int  # pack paragraphs up to about this size
    max_tokens: int  # a whole section up to this size stays one chunk
    overlap_tokens: int = 0  # trailing context carried into the next chunk of the same section


# Sizes come from the corpus: CFR sections are typically 150-180 words, so most stay whole and only
# long ones are split (at (a)/(b) boundaries). Narrative prose gets smaller chunks with a little
# overlap that never crosses a section boundary. See ADR-0002.
DEFAULT_POLICIES: dict[DocType, ChunkPolicy] = {
    DocType.REGULATION: ChunkPolicy(target_tokens=450, max_tokens=800),
    DocType.ENFORCEMENT_GUIDANCE: ChunkPolicy(target_tokens=400, max_tokens=700),
    DocType.FAQ: ChunkPolicy(target_tokens=600, max_tokens=1000),
    DocType.STANDARD: ChunkPolicy(target_tokens=400, max_tokens=650, overlap_tokens=60),
}
