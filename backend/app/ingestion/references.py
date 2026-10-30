"""Cross-reference extraction.

CFR references link the PHMSA guidance and FAQs to the regulation text they interpret, so the
retriever can pull the governing section in next to an interpretation. NIST control ids
(AC-17, SC-7...) do the same for the OT overlay.
"""

import re
from collections.abc import Iterable
from typing import Protocol

# Control families of the NIST SP 800-53 Rev. 5 catalogue, which the SP 800-82 overlay tailors.
NIST_CONTROL_FAMILIES = (
    "AC", "AT", "AU", "CA", "CM", "CP", "IA", "IR", "MA", "MP",
    "PE", "PL", "PM", "PS", "PT", "RA", "SA", "SC", "SI", "SR",
)  # fmt: skip


class ReferenceExtractor(Protocol):
    def extract(self, text: str, exclude: str | None = None) -> list[str]: ...


class CfrReferenceExtractor:
    """Section-level CFR references ("192.805") for a configured set of parts.

    A bare "195.402" is only a citation when 195 is a part we index; limiting the parts keeps
    decimals like "12.5" out. Amendment ids use a hyphen ("Amdt. 195-51") and never match.
    """

    def __init__(self, parts: Iterable[int]):
        alternatives = "|".join(str(p) for p in sorted(set(parts), reverse=True))
        if not alternatives:
            raise ValueError("at least one CFR part is required")
        self._pattern = re.compile(rf"(?<![\d.\-])({alternatives})\.(\d{{1,4}}[a-z]?)(?![\d])")

    def extract(self, text: str, exclude: str | None = None) -> list[str]:
        refs = {f"{m.group(1)}.{m.group(2)}" for m in self._pattern.finditer(text)}
        refs.discard(exclude or "")
        return sorted(refs, key=_section_sort_key)


class ControlReferenceExtractor:
    """NIST security control ids ("AC-17"), sorted by family and number."""

    def __init__(self, families: Iterable[str] = NIST_CONTROL_FAMILIES):
        self._pattern = re.compile(rf"\b({'|'.join(families)})-(\d{{1,2}})\b")

    def extract(self, text: str, exclude: str | None = None) -> list[str]:
        refs = {f"{m.group(1)}-{m.group(2)}" for m in self._pattern.finditer(text)}
        refs.discard(exclude or "")
        return sorted(refs, key=_control_sort_key)


def _section_sort_key(ref: str) -> tuple[int, int, str]:
    part, _, sec = ref.partition(".")
    digits = re.match(r"\d+", sec)
    return int(part), int(digits.group()) if digits else 0, sec


def _control_sort_key(ref: str) -> tuple[str, int]:
    family, _, num = ref.partition("-")
    return family, int(num)
