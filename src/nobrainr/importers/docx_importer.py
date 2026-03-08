"""Import .docx files (Google Docs, Nextcloud documents)."""

import logging
from pathlib import Path

from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text

logger = logging.getLogger("nobrainr")

MAX_EMBED_CHARS = 6000


def _extract_docx_text(file_path: str) -> str:
    """Extract text from a .docx file using python-docx."""
    from docx import Document
    doc = Document(file_path)
    paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs)


async def import_docx_files(
    directory: str,
    source_machine: str | None = None,
    recursive: bool = True,
) -> dict:
    """Import .docx files from a directory.

    Each document is stored as a memory with source_type="docx".
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        return {"error": f"Directory not found: {directory}"}

    pattern = "**/*.docx" if recursive else "*.docx"
    files = sorted(dir_path.glob(pattern))

    imported = 0
    skipped = 0
    errors = 0

    for docx_file in files:
        # Skip temp files
        if docx_file.name.startswith("~"):
            continue

        try:
            text = _extract_docx_text(str(docx_file))
        except Exception as e:
            logger.warning("Failed to read %s: %s", docx_file.name, e)
            errors += 1
            continue

        if not text or len(text.strip()) < 20:
            skipped += 1
            continue

        title = docx_file.stem.replace("-", " ").replace("_", " ")
        # Relative path from import directory for reference
        rel_path = str(docx_file.relative_to(dir_path))

        content = f"{title}\n\n{text}"

        try:
            embedding = await embed_text(content[:MAX_EMBED_CHARS])
        except Exception:
            logger.warning("Embedding failed for %s, skipping", docx_file.name)
            skipped += 1
            continue

        await queries.store_memory(
            content=content,
            embedding=embedding,
            summary=f"Document: {title}"[:200],
            source_type="docx",
            source_machine=source_machine,
            source_ref=rel_path,
            tags=["imported", "document", "nextcloud"],
            category="documentation",
            confidence=0.5,
            metadata={"file_path": rel_path},
        )
        imported += 1

    logger.info(
        "Docx import: %d imported, %d skipped, %d errors from %s",
        imported, skipped, errors, directory,
    )
    return {"imported": imported, "skipped": skipped, "errors": errors, "source": directory}
