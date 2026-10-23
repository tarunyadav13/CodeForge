"""Split a policy document into section-aware chunks.

Pure text processing, no I/O. Pages are separated by form feed (``\\f``), which is how
``rag.ingest`` joins PDF pages. A new chunk starts at every numbered clause heading
(``7.2 Minimum attendance``, ``Clause 7.2``, ``3. Procedure``) and at every page break,
and sections longer than ``max_words`` are split on paragraph, line, then word boundaries.
Each chunk remembers its section label and page so citations point at real text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

DEFAULT_MAX_WORDS = 300  # roughly 400 tokens

# "Clause 7.2 ...", "7.2 Heading", "7.2.1) Heading", "3. Heading". A bare "40 classes" is
# deliberately NOT a heading: plain integers need a trailing "." or ")".
_HEADING_RE = re.compile(
    r"""^\s*(?:
        (?:section|clause|article|regulation|rule)\s+(?P<kw>\d+(?:\.\d+)*)[.):]?\s+\S
      | (?P<dotted>\d+(?:\.\d+)+)[.)]?\s+[A-Za-z(]
      | (?P<plain>\d+)[.)]\s+[A-Za-z(]
    )""",
    re.IGNORECASE | re.VERBOSE,
)
_MAX_HEADING_CHARS = 160


@dataclass(frozen=True)
class Chunk:
    text: str
    section: str  # clause label such as "7.2"; "" before the first heading
    page: Optional[int]  # 1-based; None when the source has no page breaks
    index: int


def heading_label(line: str) -> Optional[str]:
    """Return the clause number if ``line`` looks like a clause heading, else None."""
    if len(line) > _MAX_HEADING_CHARS:
        return None
    m = _HEADING_RE.match(line)
    if not m:
        return None
    return m.group("kw") or m.group("dotted") or m.group("plain")


def _words(text: str) -> int:
    return len(text.split())


def _split_long(text: str, max_words: int) -> list[str]:
    """Greedy-pack paragraphs, then lines, then words, into pieces of <= max_words."""
    if _words(text) <= max_words:
        return [text]
    units: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        if _words(para) <= max_words:
            units.append(para)
            continue
        for line in para.split("\n"):  # table rows and list items stay whole when possible
            if _words(line) <= max_words:
                units.append(line)
            else:
                words = line.split()
                units.extend(
                    " ".join(words[i : i + max_words]) for i in range(0, len(words), max_words)
                )
    pieces: list[str] = []
    current: list[str] = []
    count = 0
    for unit in units:
        n = _words(unit)
        if current and count + n > max_words:
            pieces.append("\n".join(current))
            current, count = [], 0
        current.append(unit)
        count += n
    if current:
        pieces.append("\n".join(current))
    return pieces


def chunk_document(text: str, max_words: int = DEFAULT_MAX_WORDS) -> list[Chunk]:
    """Return ordered chunks; an empty or whitespace-only text gives an empty list."""
    if max_words < 20:
        raise ValueError("max_words must be at least 20")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    pages = text.split("\f")
    paged = len(pages) > 1

    chunks: list[Chunk] = []
    section = ""

    def flush(lines: list[str], page: Optional[int], label: str) -> None:
        body = "\n".join(lines).strip()
        if not body:
            return
        for piece in _split_long(body, max_words):
            piece = piece.strip()
            if piece:
                chunks.append(Chunk(text=piece, section=label, page=page, index=len(chunks)))

    for page_no, page_text in enumerate(pages, start=1):
        page = page_no if paged else None
        buffer: list[str] = []
        label = section  # a section continues across a page break
        for line in page_text.split("\n"):
            new_label = heading_label(line)
            if new_label is not None:
                flush(buffer, page, label)
                buffer, label = [line], new_label
            else:
                buffer.append(line)
        flush(buffer, page, label)
        section = label
    return chunks
