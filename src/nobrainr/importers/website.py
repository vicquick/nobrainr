"""Import website content from PHP files (my-website travel/philosophy writing)."""

import logging
import re
from pathlib import Path

from nobrainr.db import queries
from nobrainr.embeddings.ollama import embed_text

logger = logging.getLogger("nobrainr")

MAX_EMBED_CHARS = 6000


def _strip_php_html(content: str) -> tuple[str, str, str]:
    """Strip PHP/HTML tags and extract prose text + title from a PHP file.

    Returns (title, body_text).
    """
    title = ""
    # Extract $pageTitle
    title_match = re.search(r"\$pageTitle\s*=\s*['\"](.+?)['\"]", content)
    if title_match:
        title = title_match.group(1)

    # Extract section from $activeNavLink
    section_match = re.search(r"\$activeNavLink\s*=\s*['\"](.+?)['\"]", content)
    section = section_match.group(1) if section_match else ""

    # Remove PHP blocks
    text = re.sub(r"<\?php.*?\?>", "", content, flags=re.DOTALL)

    # Remove script and style blocks
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # Remove HTML tags but keep content
    text = re.sub(r"<[^>]+>", " ", text)

    # Decode HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&quot;", '"')

    # Clean up whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # Remove common boilerplate
    text = re.sub(r"Image Description", "", text)

    if not title:
        # Try to extract from h1/h2/h3 content
        h_match = re.search(r"<h[123][^>]*>(.*?)</h[123]>", content, re.DOTALL | re.IGNORECASE)
        if h_match:
            title = re.sub(r"<[^>]+>", "", h_match.group(1)).strip()

    return title, text, section


async def import_website_content(
    directory: str,
    source_machine: str | None = None,
    website_name: str = "my-website",
) -> dict:
    """Import PHP content files from a website directory.

    Filenames are expected as section__page.php (e.g. philosophy__valueoftime.php).
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        return {"error": f"Directory not found: {directory}"}

    files = sorted(dir_path.glob("*.php"))

    imported = 0
    skipped = 0

    for php_file in files:
        raw = php_file.read_text(encoding="utf-8", errors="replace")
        title, body, section = _strip_php_html(raw)

        if not body or len(body.strip()) < 50:
            skipped += 1
            continue

        # Parse section from filename (section__page.php)
        name_parts = php_file.stem.split("__")
        if len(name_parts) == 2 and not section:
            section = name_parts[0]

        if not title:
            title = name_parts[-1].replace("-", " ").title()

        content = f"{title}\n\n{body}"
        tags = ["imported", "website", website_name.replace(".", "-")]
        if section:
            tags.append(section.lower())

        # Determine category from section
        cat_map = {
            "philosophy": "insight",
            "australia": "documentation",
            "thailand": "documentation",
            "laos": "documentation",
            "cambodia": "documentation",
            "georgia": "documentation",
            "philippines": "documentation",
            "netherlands": "documentation",
            "kualalumpur": "documentation",
            "ethnography": "insight",
        }
        category = cat_map.get(section.lower(), "documentation")

        try:
            embedding = await embed_text(content[:MAX_EMBED_CHARS])
        except Exception:
            logger.warning("Embedding failed for %s, skipping", php_file.name)
            skipped += 1
            continue

        await queries.store_memory(
            content=content,
            embedding=embedding,
            summary=f"{website_name}: {title}"[:200],
            source_type="website",
            source_machine=source_machine,
            source_ref=f"{website_name}/{section}/{php_file.stem}",
            tags=tags,
            category=category,
            confidence=0.6,
            metadata={
                "website": website_name,
                "section": section,
                "filename": php_file.name,
            },
        )
        imported += 1

    logger.info(
        "Website import: %d imported, %d skipped from %s",
        imported, skipped, directory,
    )
    return {"imported": imported, "skipped": skipped, "source": directory}
