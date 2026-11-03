"""Turning the PDFs into retrievable chunks.

Chunking is the decision that most determines retrieval quality, and it is
the one tutorials hand-wave by splitting every N characters. Fixed-size
splitting cuts sentences in half and mixes two unrelated ideas into one
vector, which blurs the embedding and makes similarity meaningless.

These documents have real structure, so we split on it:

    Theme 1: Earn                 <- section heading
    Topic Connections: ...        <- cross-references, low retrieval value
    Topic Overview: ...           <- the concept, one chunk
    From the classroom:
      How do you know which career is right for you? ...   <- one chunk
      What is an entrepreneur? ...                         <- one chunk

Each "From the classroom" entry is a question followed by its answer, which
is exactly the unit a parent is searching for ("how do I explain X?"), so
those become chunks of their own. That single decision does more for
retrieval precision than any amount of embedding-model tuning.

Every chunk carries the age band, taken from the FILENAME and never from the
document text: `fdic-gr6-8-parent-guide-module-1.pdf` has
"GRADES PRE-K -2" in its own header, so FDIC shipped the wrong one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# FDIC grade band -> the age groups this app already writes stories for.
# The overlap is deliberate: a 6-year-old sits across two FDIC bands, so a
# search for ages 6-9 should see both.
BAND_TO_AGE_GROUPS: dict[str, tuple[str, ...]] = {
    "prek-2": ("3-5", "6-9"),
    "gr3-5": ("6-9", "10-12"),
    "gr6-8": ("10-12", "13-17"),
    "gr9-12": ("13-17",),
}
BANDS = tuple(BAND_TO_AGE_GROUPS)

# Repeated on every page; pure noise that would otherwise dominate short
# chunks and make unrelated pages look similar to each other.
_PAGE_FURNITURE = re.compile(
    r"^\s*MONEY SMART GRADES?[^\n]*Parent/Caregiver Guide\s*\d*\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Headings that start a new section.
_SECTION = re.compile(
    r"^\s*(?P<head>"
    r"Theme\s+\d+\s*:\s*[^\n]*"
    r"|Lesson\s+\d+\s*:\s*[^\n]*"
    r"|TOPIC\s*:\s*[^\n]*"
    r"|Topic\s+Overview\s*:"
    r"|Topic\s+Connections\s*:"
    r"|About\s+This\s+Topic\s*:"
    r"|From\s+the\s+classroom\s*:"
    r"|Try\s+This[^\n]*"
    r"|Activity[^\n]*"
    r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# A Q&A entry is a question, then its answer, until the next question.
#
# The hard part is telling a new entry from a question mark *inside* an
# answer — answers routinely echo the question ("...right for you? Finding
# the right path begins..."). The reliable signal is what precedes the
# candidate: a new entry follows the END of the previous answer, which is a
# full stop or exclamation mark. A '?' before it means we are still inside
# one answer, so splitting there would cut it in half.
_QUESTION = re.compile(r"(?<=[.!])\s+(?=[A-Z][^.?!\n]{10,120}\?)")

# Sections whose content is cross-references rather than teaching material.
_LOW_VALUE_SECTIONS = ("topic connections",)

# Chunks shorter than this are fragments (stray headings, page numbers) and
# only add noise to the index.
MIN_CHUNK_CHARS = 120
# Above this a chunk covers more than one idea, so it gets sub-split.
MAX_CHUNK_CHARS = 1800


@dataclass(frozen=True)
class Chunk:
    """One retrievable unit, with everything needed to filter and cite it."""

    text: str
    source_file: str
    band: str
    section: str
    kind: str  # "overview" | "qa" | "activity" | "prose"
    age_groups: tuple[str, ...] = field(default=())

    @property
    def chunk_id(self) -> str:
        """Stable id, so re-ingesting replaces rather than duplicates."""
        from hashlib import sha256

        digest = sha256(f"{self.source_file}|{self.text}".encode()).hexdigest()
        return digest[:16]

    @property
    def embed_text(self) -> str:
        """What actually gets embedded.

        The section heading is prepended because a bare answer often never
        names its own topic — "An entrepreneur is someone who..." embeds
        far closer to a query about entrepreneurship when the chunk also
        carries "Theme 1: Earn".
        """
        head = f"[Ages {'/'.join(self.age_groups)}] {self.section}".strip()
        return f"{head}\n{self.text}"

    def estimated_tokens(self) -> int:
        return max(1, len(self.embed_text) // 4)


def band_from_filename(name: str) -> str | None:
    """Age band from the filename.

    Deliberately not from the document text: at least one FDIC file carries
    the wrong band in its own page header.
    """
    stem = Path(name).name.lower()
    # Longest first, so "gr3-5" can't match inside "gr9-12"-style names.
    for band in sorted(BANDS, key=len, reverse=True):
        if band in stem:
            return band
    return None


def strip_page_furniture(text: str) -> str:
    """Drop repeated page headers and collapse whitespace."""
    text = _PAGE_FURNITURE.sub("", text)
    # PDF extraction leaves trailing spaces and hyphenated line breaks.
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_sections(text: str) -> list[tuple[str, str]]:
    """`[(heading, body)]` in document order.

    Text before the first heading is returned under an empty heading rather
    than dropped — the first page of the general guides has no headings at
    all and is still worth indexing.
    """
    matches = list(_SECTION.finditer(text))
    if not matches:
        return [("", text)] if text.strip() else []
    out: list[tuple[str, str]] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        out.append(("", preamble))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        head = re.sub(r"\s*:\s*$", "", m.group("head").strip())
        # Headings with an empty body are kept, not dropped. "Lesson 1: Money
        # Matters" is immediately followed by "TOPIC: Buying Decisions", so
        # both have no body of their own — but they name the topic every
        # later chunk in the document needs. Dropping them here left every
        # chunk anonymous, which silently degraded embedding quality.
        out.append((head, text[m.end() : end].strip()))
    return out


def split_qa(body: str) -> list[str]:
    """Split a "From the classroom" block into one entry per question."""
    parts = [p.strip() for p in _QUESTION.split(body) if p.strip()]
    # If the split found nothing to do, the block is a single entry.
    return parts or ([body.strip()] if body.strip() else [])


def _sub_split(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Break an oversized block on paragraph, then sentence boundaries.

    No overlap: these chunks are already one topic each, and overlap would
    return near-duplicates in the results, wasting the context budget the
    retrieval exists to protect.
    """
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    buf = ""
    units = re.split(r"\n\n+", text)
    if max((len(u) for u in units), default=0) > limit:
        units = re.split(r"(?<=[.!?])\s+", text)
    # Last resort: a single unit still over the limit has no paragraph or
    # sentence boundary to use — a table, or a list with no punctuation.
    # Without this the block passes through whole and one chunk silently
    # dominates the index.
    if max((len(u) for u in units), default=0) > limit:
        units = [u[i : i + limit] for u in units for i in range(0, len(u), limit)]
    for unit in units:
        if buf and len(buf) + len(unit) + 1 > limit:
            out.append(buf.strip())
            buf = unit
        else:
            buf = f"{buf} {unit}".strip() if buf else unit
    if buf.strip():
        out.append(buf.strip())
    return out


def _kind_for(heading: str) -> str:
    h = heading.lower()
    if "classroom" in h:
        return "qa"
    if "overview" in h or "about this topic" in h:
        return "overview"
    if "try this" in h or "activity" in h:
        return "activity"
    return "prose"


def chunk_document(text: str, source_file: str, band: str | None = None) -> list[Chunk]:
    """Chunk one document's extracted text."""
    band = band or band_from_filename(source_file)
    if band is None:
        raise ValueError(f"cannot determine age band from filename: {source_file}")
    age_groups = BAND_TO_AGE_GROUPS[band]
    clean = strip_page_furniture(text)

    chunks: list[Chunk] = []
    current_theme = ""
    for heading, body in split_sections(clean):
        h = heading.lower()
        # A Theme/Lesson/TOPIC line names what follows; carry it down so
        # later chunks aren't anonymous.
        if h.startswith(("theme", "lesson")) or h.startswith("topic:"):
            current_theme = heading
        if any(s in h for s in _LOW_VALUE_SECTIONS):
            continue
        kind = _kind_for(heading)
        section = f"{current_theme} — {heading}".strip(" —") if heading else current_theme
        pieces = split_qa(body) if kind == "qa" else [body]
        for piece in pieces:
            for part in _sub_split(piece):
                if len(part) < MIN_CHUNK_CHARS:
                    continue
                chunks.append(Chunk(
                    text=part,
                    source_file=Path(source_file).name,
                    band=band,
                    section=section or "(untitled)",
                    kind=kind,
                    age_groups=age_groups,
                ))
    return chunks


def load_pdf_text(path: str | Path) -> str:
    """Extract text from one PDF. Imported lazily so pypdf stays optional."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def chunk_selected(directory: str | Path) -> list[Chunk]:
    """Chunk the curated corpus only — see `app.services.rag.corpus`.

    This is what ingestion should call. `chunk_corpus` takes everything in
    the directory, which includes the credit, insurance and boilerplate
    documents that only dilute retrieval.
    """
    from app.services.rag.corpus import BAND_OVERRIDES, SELECTED_FILENAMES

    out: list[Chunk] = []
    for name in SELECTED_FILENAMES:
        path = Path(directory) / name
        if not path.exists():
            raise FileNotFoundError(f"curated source missing: {path}")
        out.extend(chunk_document(
            load_pdf_text(path), name, band=BAND_OVERRIDES.get(name),
        ))
    return out


def chunk_corpus(directory: str | Path, only: list[str] | None = None) -> list[Chunk]:
    """Chunk every PDF in `directory` whose name is in `only` (or all).

    Files whose band can't be determined are skipped with no exception: the
    directory is user-managed and an unrelated PDF landing there shouldn't
    break ingestion.
    """
    out: list[Chunk] = []
    for pdf in sorted(Path(directory).glob("*.pdf")):
        if only and pdf.name not in only:
            continue
        if band_from_filename(pdf.name) is None:
            continue
        out.extend(chunk_document(load_pdf_text(pdf), pdf.name))
    return out
