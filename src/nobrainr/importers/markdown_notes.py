"""Import markdown notes with YAML frontmatter (Google Keep, Affine memos)."""

import logging
from pathlib import Path

from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text

logger = logging.getLogger("nobrainr")

MAX_EMBED_CHARS = 6000


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from markdown text. Returns (metadata, body)."""
    if not text.startswith("---"):
        return {}, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text

    import yaml
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except Exception:
        meta = {}

    body = parts[2].strip()
    return meta, body


async def import_markdown_notes(
    directory: str,
    source_type: str,
    source_machine: str | None = None,
) -> dict:
    """Import markdown notes from a directory.

    Each .md file is parsed for YAML frontmatter and stored as a memory.

    Args:
        directory: Path to directory containing .md files.
        source_type: E.g. "google_keep", "affine_memos".
        source_machine: Machine identifier.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        return {"error": f"Directory not found: {directory}"}

    files = sorted(dir_path.glob("*.md"))
    imported = 0
    skipped = 0

    for md_file in files:
        text = md_file.read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_frontmatter(text)

        if not body or len(body.strip()) < 10:
            skipped += 1
            continue

        title = meta.get("title", md_file.stem.replace("-", " ").replace("_", " "))
        created = meta.get("created")
        category_raw = meta.get("category", "")
        source = meta.get("source", source_type)
        pinned = meta.get("pinned", False)
        archived = meta.get("archived", False)

        tags = ["imported", source_type.replace("_", "-")]
        if pinned:
            tags.append("pinned")
        if archived:
            tags.append("archived")
        if category_raw and category_raw != "uncategorized":
            tags.append(category_raw)

        # Build content with title prefix for better embeddings
        content = f"{title}\n\n{body}" if title else body

        # Summary: first meaningful line (skip empty or very short)
        summary = title[:200] if title else body.split("\n")[0][:200]

        try:
            embedding = await embed_text(content[:MAX_EMBED_CHARS])
        except Exception:
            logger.warning("Embedding failed for %s, skipping", md_file.name)
            skipped += 1
            continue

        metadata = {"source_file": md_file.name}
        if created:
            metadata["original_date"] = str(created)
        if meta.get("memo_id"):
            metadata["memo_id"] = meta["memo_id"]

        await queries.store_memory(
            content=content,
            embedding=embedding,
            summary=summary,
            source_type=source_type,
            source_machine=source_machine,
            source_ref=md_file.name,
            tags=tags,
            category="documentation",
            confidence=0.5,
            metadata=metadata,
        )
        imported += 1

    logger.info(
        "%s import: %d imported, %d skipped from %s",
        source_type, imported, skipped, directory,
    )
    return {"imported": imported, "skipped": skipped, "source": directory}
