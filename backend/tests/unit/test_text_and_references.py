import pytest

from app.ingestion.references import CfrReferenceExtractor
from app.ingestion.text import (
    build_vocabulary,
    estimate_tokens,
    join_lines,
    normalize,
    slugify,
    split_sentences,
    squash,
    tail_tokens,
)


def test_normalize_unifies_quotes_ligatures_and_whitespace():
    assert normalize("The operator’s  ﬁle is “final”") == 'The operator\'s file is "final"'


def test_join_lines_repairs_hyphenation_only_for_known_words():
    vocab = build_vocabulary(["the maximum operating pressure"])
    assert join_lines(["below the maxi-", "mum pressure"], vocab) == "below the maximum pressure"
    # "connectionspecific" never occurs, so the hyphen is a real compound
    assert join_lines(["connection-", "specific authentication"], vocab) == "connection-specific authentication"


def test_join_lines_keeps_identifier_hyphens():
    assert join_lines(["Amdt. 195-", "51 applies"], set()) == "Amdt. 195-51 applies"


def test_split_sentences_does_not_break_section_numbers():
    sentences = split_sentences("See 49 CFR 195.402(c). Operators must comply. Records are kept.")
    assert sentences == ["See 49 CFR 195.402(c).", "Operators must comply.", "Records are kept."]


def test_tail_tokens_returns_whole_trailing_sentences():
    text = "First sentence here. Second one. Third and last."
    assert tail_tokens(text, 8) == "Second one. Third and last."


def test_estimate_tokens_counts_words_and_punctuation():
    assert estimate_tokens("Hello, world.") == 4


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("(§§ 192.805(b), (c); 195.505(b), (c))", ["192.805", "195.505"]),
        ("see 49 CFR 192.327 and 195.248", ["192.327", "195.248"]),
        ("per §195.402(c)(13) and § 195.452", ["195.402", "195.452"]),
        ("Amdt. 195-51, 59 FR 29379", []),
        ("a pressure of 1950.5 psig", []),
    ],
)
def test_extract_cfr_refs(references, text, expected):
    assert references.cfr.extract(text) == expected


def test_extract_cfr_refs_can_exclude_own_section(references):
    assert references.cfr.extract("as required by § 195.402 and § 195.404", exclude="195.402") == ["195.404"]


def test_cfr_parts_are_configurable():
    assert CfrReferenceExtractor([40]).extract("see 40.12 and 195.402") == ["40.12"]
    with pytest.raises(ValueError):
        CfrReferenceExtractor([])


def test_extract_control_refs_sorted_by_family_and_number(references):
    assert references.controls.extract("See SC-45, AC-17 (9) and SC-7; not XX-1") == ["AC-17", "SC-7", "SC-45"]


def test_slugify_and_squash():
    assert slugify("Emergency flow restricting device or EFRD") == "emergency-flow-restricting-device-or-efrd"
    assert squash("2.3.2. SCADA Systems") == squash("232 scada systems")
