"""Import Windows Sticky Notes from OneDrive CSV export."""

import csv
import logging

from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text

logger = logging.getLogger("nobrainr")

MAX_EMBED_CHARS = 6000


async def import_sticky_notes(
    file_path: str,
    source_machine: str | None = None,
) -> dict:
    """Import sticky notes from CSV export.

    CSV columns: Note Body, Categories, Note Color, Priority, Sensitivity
    Each note is stored directly as a memory (no distillation needed).
    """
    with open(file_path, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        notes = list(reader)

    imported = 0
    skipped = 0

    for note in notes:
        body = note.get("Note Body", "").strip()
        if not body or len(body) < 10:
            skipped += 1
            continue

        color = note.get("Note Color", "").strip()
        categories = note.get("Categories", "").strip()
        priority = note.get("Priority", "").strip()

        # Build tags from CSV metadata
        tags = ["imported", "sticky-notes"]
        if color:
            tags.append(f"color:{color.lower()}")
        if categories:
            tags.extend(c.strip().lower() for c in categories.split(",") if c.strip())

        # First line as summary (truncate to 200 chars)
        first_line = body.split("\n")[0].strip()[:200]

        try:
            embedding = await embed_text(body[:MAX_EMBED_CHARS])
        except Exception:
            logger.warning("Embedding failed for sticky note, skipping")
            skipped += 1
            continue

        await queries.store_memory(
            content=body,
            embedding=embedding,
            summary=first_line,
            source_type="sticky_notes",
            source_machine=source_machine or "workpc",
            tags=tags,
            category="documentation",
            confidence=0.5,
            metadata={
                "note_color": color,
                "priority": priority,
            },
        )
        imported += 1

    logger.info(
        "Sticky notes import: %d imported, %d skipped from %s",
        imported, skipped, file_path,
    )
    return {"imported": imported, "skipped": skipped, "source": file_path}
