"""Text normalisation helpers shared by all parsers."""

import re
import unicodedata
from collections.abc import Iterable

_CHAR_MAP = str.maketrans(
    {
        "­": "",  # soft hyphen
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "⁄": "/",  # fraction slash: 5⁄8 -> 5/8
        "": "•",  # Symbol-font bullet
        "": "•",
        "▪": "•",
    }
)
_WS = re.compile(r"[ \t\f\v ]+")
_MIXED_FRACTION = re.compile(r"(\d)([¼-¾⅐-⅞])")
_TOKEN = re.compile(r"\w+|[^\w\s]")
_WORD = re.compile(r"[A-Za-z]+")


def normalize(text: str) -> str:
    """NFKC-normalise, unify quotes/bullets, collapse horizontal whitespace."""
    text = _MIXED_FRACTION.sub(r"\1 \2", text)  # "7½" -> "7 ½" so NFKC yields "7 1/2", not "71/2"
    text = unicodedata.normalize("NFKC", text).translate(_CHAR_MAP)
    text = _WS.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def estimate_tokens(text: str) -> int:
    """Deterministic, dependency-free token estimate (words + punctuation).

    Within ~10-15% of BPE tokenizers on English regulatory prose, which is plenty for chunk sizing:
    the embedding model's context (32k) is far above any chunk we produce.
    """
    return len(_TOKEN.findall(text))


def build_vocabulary(texts: Iterable[str]) -> set[str]:
    vocab: set[str] = set()
    for t in texts:
        vocab.update(w.lower() for w in _WORD.findall(t))
    return vocab


def join_lines(lines: Iterable[str], vocab: set[str] | None = None) -> str:
    """Join wrapped PDF lines into one paragraph, repairing end-of-line hyphenation.

    "mini-" + "mum" becomes "minimum" only if "minimum" occurs elsewhere in the document;
    genuine compounds such as "connection-" + "specific" keep their hyphen.
    """
    out = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not out:
            out = line
            continue
        m = re.search(r"([A-Za-z]+)-$", out)
        nxt = re.match(r"([a-z]+)", line)
        if re.search(r"\d-$", out) and line[:1].isdigit():
            out = out + line  # identifier wrapped at its hyphen: "Amdt. 195-" + "51"
        elif m and nxt:
            merged = (m.group(1) + nxt.group(1)).lower()
            if vocab is not None and merged in vocab:
                out = out[:-1] + line
            else:
                out = out + line
        else:
            out = out + " " + line
    return _WS.sub(" ", out).strip()


_SENTENCE_END = re.compile(r"(?<=[.;:?!])\s+(?=[\"'(\[]?[A-Z0-9])")


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE_END.split(text)]
    return [p for p in parts if p]


def tail_tokens(text: str, max_tokens: int) -> str:
    """Trailing whole sentences of `text` that fit in `max_tokens` (used for chunk overlap)."""
    picked: list[str] = []
    used = 0
    for sentence in reversed(split_sentences(text)):
        n = estimate_tokens(sentence)
        if used + n > max_tokens:
            break
        picked.insert(0, sentence)
        used += n
    return " ".join(picked)
