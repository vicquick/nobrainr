"""Text chunking utilities for document ingestion.

Splits long text into overlapping chunks at natural boundaries
(paragraphs, then lines, then hard cut). Each chunk carries its
position metadata so callers can reconstruct ordering and link
chunks to a parent document.
"""

from __future__ import annotations

from dataclasses import dataclass

from nobrainr.config import settings


@dataclass
class Chunk:
    """A single chunk of a larger document."""

    text: str
    index: int  # 0-based
    total: int
    char_offset: int  # offset in the original text


def chunk_text(
    text: str,
    *,
    max_chars: int | None = None,
    overlap: int | None = None,
) -> list[Chunk]:
    """Split *text* into overlapping chunks at natural boundaries.

    Args:
        text: The full document text.
        max_chars: Maximum characters per chunk (default from settings).
        overlap: Characters of overlap between consecutive chunks (default from settings).

    Returns:
        List of Chunk objects.  If the text is short enough, returns a
        single chunk with index=0, total=1.
    """
    max_chars = max_chars or settings.chunk_max_chars
    overlap = overlap or settings.chunk_overlap_chars

    text = text.strip()
    if not text:
        return []

    if len(text) <= max_chars:
        return [Chunk(text=text, index=0, total=1, char_offset=0)]

    chunks: list[Chunk] = []
    pos = 0

    while pos < len(text):
        end = min(pos + max_chars, len(text))
        segment = text[pos:end]

        # If we're not at the very end, try to break at a natural boundary
        if end < len(text):
            # Prefer paragraph break
            cut = segment.rfind("\n\n")
            if cut > max_chars // 3:
                end = pos + cut
            else:
                # Fall back to single newline
                cut = segment.rfind("\n")
                if cut > max_chars // 3:
                    end = pos + cut
                else:
                    # Fall back to sentence end
                    for sep in (". ", "! ", "? "):
                        cut = segment.rfind(sep)
                        if cut > max_chars // 3:
                            end = pos + cut + len(sep)
                            break

        chunk_text_str = text[pos:end].strip()
        if chunk_text_str:
            chunks.append(Chunk(text=chunk_text_str, index=len(chunks), total=0, char_offset=pos))

        # Advance with overlap
        next_pos = end - overlap
        if next_pos <= pos:
            # Prevent infinite loop: always advance at least 1 char past overlap
            next_pos = end
        pos = next_pos

    # Backfill total count
    for c in chunks:
        c.total = len(chunks)

    return chunks
