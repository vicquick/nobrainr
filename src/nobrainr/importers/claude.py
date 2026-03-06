"""Claude memory file import pipeline."""

import logging
from pathlib import Path

from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text

logger = logging.getLogger("nobrainr.import.claude")


async def import_claude_memory(directory: str, *, machine_name: str | None = None) -> dict:
    """Import Claude memory and project files from a .claude directory.

    Scans for:
    - CLAUDE.md files (project instructions)
    - memory/ directories (auto-memory files)
    - projects/*/memory/ (project-specific memories)
    """
    base = Path(directory)
    if not base.exists():
        return {"error": f"Directory not found: {directory}"}

    imported = 0
    errors = 0

    # Import CLAUDE.md files
    for claude_md in _find_claude_md_files(base):
        try:
            content = claude_md.read_text()
            if len(content.strip()) < 10:
                continue

            embedding = await embed_text(content)
            rel_path = str(claude_md.relative_to(base))

            await queries.store_memory(
                content=content,
                embedding=embedding,
                summary=f"CLAUDE.md: {rel_path}",
                source_type="claude",
                source_machine=machine_name,
                source_ref=str(claude_md),
                tags=["imported", "claude", "instructions", "claude-md"],
                category="project-config",
                confidence=0.9,
            )
            imported += 1
        except Exception as e:
            logger.warning(f"Failed to import {claude_md}: {e}")
            errors += 1

    # Import memory files
    for memory_file in _find_memory_files(base):
        try:
            content = memory_file.read_text()
            if len(content.strip()) < 10:
                continue

            embedding = await embed_text(content)
            rel_path = str(memory_file.relative_to(base))

            # Determine project from path
            project = _extract_project_name(rel_path)
            tags = ["imported", "claude", "memory"]
            if project:
                tags.append(f"project:{project}")

            await queries.store_memory(
                content=content,
                embedding=embedding,
                summary=f"Claude memory: {rel_path}",
                source_type="claude",
                source_machine=machine_name,
                source_ref=str(memory_file),
                tags=tags,
                category="learned-pattern",
                confidence=0.8,
                metadata={"project": project} if project else None,
            )
            imported += 1
        except Exception as e:
            logger.warning(f"Failed to import {memory_file}: {e}")
            errors += 1

    return {
        "status": "complete",
        "memories_imported": imported,
        "errors": errors,
        "source_directory": directory,
        "machine": machine_name,
    }


def _find_claude_md_files(base: Path) -> list[Path]:
    """Find all CLAUDE.md files under the base directory."""
    results = []
    # Direct CLAUDE.md
    for pattern in ["CLAUDE.md", "*/CLAUDE.md", "projects/*/CLAUDE.md"]:
        results.extend(base.glob(pattern))
    return sorted(set(results))


def _find_memory_files(base: Path) -> list[Path]:
    """Find all memory markdown files."""
    results = []
    for pattern in ["memory/*.md", "projects/*/memory/*.md"]:
        results.extend(base.glob(pattern))
    return sorted(set(results))


def _extract_project_name(rel_path: str) -> str | None:
    """Extract project name from relative path like projects/foo/memory/bar.md."""
    parts = Path(rel_path).parts
    if "projects" in parts:
        idx = list(parts).index("projects")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None
