"""Streams one string field out of a structured-output JSON object while it is still arriving.

The answer model returns {"answer": "...", "sufficient_evidence": ...}. To show the answer as it
is written, the decoded value of "answer" is emitted chunk by chunk; escapes split across chunks
(\\n, \\", \\u00a7) are held back until complete.
"""

import json
import re

_HIGH_SURROGATE_PREFIXES = {"d8", "d9", "da", "db"}  # \uD800-\uDBFF


class JsonStringFieldStream:
    def __init__(self, field: str):
        self._key = re.compile(rf'"{re.escape(field)}"\s*:\s*"')
        self._buffer = ""
        self._emitted = 0  # decoded characters already returned

    def reset(self) -> None:
        self._buffer = ""
        self._emitted = 0

    def feed(self, chunk: str) -> str:
        """Add raw JSON text; return the newly decoded part of the field's value ("" if none)."""
        self._buffer += chunk
        match = self._key.search(self._buffer)
        if not match:
            return ""
        decoded: str = json.loads('"' + _complete_prefix(self._buffer[match.end() :]) + '"')
        new = decoded[self._emitted :]
        self._emitted = len(decoded)
        return new


def _complete_prefix(raw: str) -> str:
    """The longest prefix of a JSON string body that decodes on its own (stops at the closing quote)."""
    i = 0
    while i < len(raw):
        if raw[i] == '"':
            return raw[:i]
        if raw[i] == "\\":
            width = 6 if raw[i + 1 : i + 2] == "u" else 2  # \uXXXX or \n, \", \\ ...
            if width == 6 and raw[i + 2 : i + 4].lower() in _HIGH_SURROGATE_PREFIXES:
                width = 12  # a surrogate pair (🛢) only decodes as a whole
            if i + width > len(raw):
                return raw[:i]  # escape not complete yet
            i += width
        else:
            i += 1
    return raw
