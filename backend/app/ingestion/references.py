"""Cross-reference extraction.

CFR references link the PHMSA guidance and FAQs to the regulation text they interpret, so the
retriever can pull the governing section in next to an interpretation. NIST control ids
(AC-17, SC-7...) do the same for the OT overlay.
"""

import re

# Parts 190-199 of 49 CFR are the pipeline safety regulations. A bare "195.402" in this corpus is
# always a section reference; amendment ids use a hyphen ("Amdt. 195-51") and do not match.
_CFR_SECTION = re.compile(r"(?<![\d.\-])(19\d)\.(\d{1,4}[a-z]?)(?![\d])")

_CONTROL_FAMILIES = "AC|AT|AU|CA|CM|CP|IA|IR|MA|MP|PE|PL|PM|PS|PT|RA|SA|SC|SI|SR"
_CONTROL = re.compile(rf"\b({_CONTROL_FAMILIES})-(\d{{1,2}})\b")


def extract_cfr_refs(text: str, exclude: str | None = None) -> list[str]:
    """Section-level references such as "192.805", sorted and de-duplicated."""
    refs = {f"{m.group(1)}.{m.group(2)}" for m in _CFR_SECTION.finditer(text)}
    if exclude:
        refs.discard(exclude)
    return sorted(refs, key=_section_sort_key)


def extract_control_refs(text: str) -> list[str]:
    return sorted({f"{m.group(1)}-{m.group(2)}" for m in _CONTROL.finditer(text)}, key=_control_sort_key)


def _section_sort_key(ref: str) -> tuple[int, int, str]:
    part, _, sec = ref.partition(".")
    digits = re.match(r"\d+", sec)
    return int(part), int(digits.group()) if digits else 0, sec


def _control_sort_key(ref: str) -> tuple[str, int]:
    family, _, num = ref.partition("-")
    return family, int(num)
