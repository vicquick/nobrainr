"""Universal document importer — extract text from PDFs, images, DOCX, and markdown.

Uses pymupdf for PDF text extraction and page-to-image rendering.
Uses gemma3:12b vision (via Ollama) for scanned/image-only PDFs and image files.
Uses python-docx for .docx files.
Plain text/markdown files are read directly.

Flow:
  walk directory → detect file type → extract text → chunk if needed
  → store_memory_with_extraction() → entities + relations
"""

import asyncio
import base64
import logging
from pathlib import Path

from nobrainr.config import settings
from nobrainr.services.memory import store_memory_with_extraction

logger = logging.getLogger("nobrainr.import.documents")

# File extensions we handle
TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".rst", ".org", ".csv", ".tsv", ".log"}
DOCX_EXTENSIONS = {".docx"}
PDF_EXTENSIONS = {".pdf"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}

ALL_EXTENSIONS = TEXT_EXTENSIONS | DOCX_EXTENSIONS | PDF_EXTENSIONS | IMAGE_EXTENSIONS

# Chunk size for splitting large documents into separate memories
MAX_MEMORY_CHARS = 6000
# Minimum content length to bother storing
MIN_CONTENT_CHARS = 30
# Max chars to send to vision model per page
MAX_VISION_CHARS = 3000


async def _vision_extract(image_bytes: bytes, prompt: str = "Extract all text and information from this document page. Preserve structure, headings, and formatting.") -> str:
    """Send an image to gemma3 vision via Ollama and get text back."""
    import httpx

    b64 = base64.b64encode(image_bytes).decode("ascii")
    payload = {
        "model": settings.extraction_model,
        "messages": [
            {"role": "user", "content": prompt, "images": [b64]},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.1, "num_ctx": 4096},
        "keep_alive": "5m",
    }
    async with httpx.AsyncClient(base_url=settings.ollama_url, timeout=120.0) as client:
        for attempt in range(3):
            try:
                resp = await client.post("/api/chat", json=payload)
                if resp.status_code in {404, 503, 502}:
                    wait = 2 ** attempt
                    logger.warning("Ollama vision %d (attempt %d/3), retrying in %ds", resp.status_code, attempt + 1, wait)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                return data.get("message", {}).get("content", "")
            except (httpx.ConnectError, httpx.ReadTimeout) as exc:
                wait = 2 ** attempt
                logger.warning("Ollama vision connection error: %s (attempt %d/3)", exc, attempt + 1)
                await asyncio.sleep(wait)
    return ""


def _extract_text_file(path: Path) -> str:
    """Read a plain text/markdown file."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning("Cannot read text file %s: %s", path.name, e)
        return ""


def _extract_docx(path: Path) -> str:
    """Extract text from a .docx file."""
    from docx import Document
    try:
        doc = Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs)
    except Exception as e:
        logger.warning("Cannot read docx %s: %s", path.name, e)
        return ""


def _extract_pdf_text(path: Path) -> tuple[str, bool]:
    """Extract text from a PDF. Returns (text, has_text).

    has_text=False means the PDF is likely scanned/image-only and needs vision.
    """
    import pymupdf

    try:
        doc = pymupdf.open(str(path))
    except Exception as e:
        logger.warning("Cannot open PDF %s: %s", path.name, e)
        return "", False

    text_parts = []
    for page in doc:
        text = page.get_text("text")
        if text.strip():
            text_parts.append(text.strip())
    doc.close()

    full_text = "\n\n".join(text_parts)
    has_text = len(full_text.strip()) > MIN_CONTENT_CHARS
    return full_text, has_text


async def _extract_pdf_vision(path: Path) -> str:
    """Render PDF pages to images and extract text via vision model."""
    import pymupdf

    try:
        doc = pymupdf.open(str(path))
    except Exception as e:
        logger.warning("Cannot open PDF for vision %s: %s", path.name, e)
        return ""

    parts = []
    # Process up to 20 pages to avoid overwhelming the LLM
    for i, page in enumerate(doc):
        if i >= 20:
            parts.append(f"\n[... {len(doc) - 20} more pages not processed]")
            break
        # Render page at 150 DPI for good OCR quality without huge images
        pix = page.get_pixmap(dpi=150)
        img_bytes = pix.tobytes("png")
        text = await _vision_extract(img_bytes)
        if text.strip():
            parts.append(f"--- Page {i + 1} ---\n{text.strip()}")

    doc.close()
    return "\n\n".join(parts)


async def _extract_image_vision(path: Path) -> str:
    """Extract text/information from an image file via vision model."""
    try:
        img_bytes = path.read_bytes()
    except Exception as e:
        logger.warning("Cannot read image %s: %s", path.name, e)
        return ""

    # Validate size (skip images > 20MB)
    if len(img_bytes) > 20 * 1024 * 1024:
        logger.warning("Image too large (%d MB), skipping: %s", len(img_bytes) // (1024 * 1024), path.name)
        return ""

    return await _vision_extract(
        img_bytes,
        prompt="Describe this image in detail. If it contains text, extract all text. If it's a diagram, describe the structure and relationships shown.",
    )


def _chunk_text(text: str, title: str, max_chars: int = MAX_MEMORY_CHARS) -> list[str]:
    """Split long text into chunks, each prefixed with title."""
    if len(text) <= max_chars:
        return [f"{title}\n\n{text}"]

    chunks = []
    remaining = text
    part = 1
    while remaining:
        # Try to split at paragraph boundary
        chunk = remaining[:max_chars]
        if len(remaining) > max_chars:
            # Find last paragraph break within limit
            last_para = chunk.rfind("\n\n")
            if last_para > max_chars // 3:
                chunk = remaining[:last_para]
            else:
                # Fall back to line break
                last_line = chunk.rfind("\n")
                if last_line > max_chars // 3:
                    chunk = remaining[:last_line]

        chunk_title = f"{title} (part {part})" if part > 1 else title
        chunks.append(f"{chunk_title}\n\n{chunk.strip()}")
        remaining = remaining[len(chunk):].strip()
        part += 1

    return chunks


async def import_documents(
    directory: str,
    *,
    source_machine: str | None = None,
    recursive: bool = True,
    use_vision: bool = True,
    extensions: set[str] | None = None,
    category: str = "documentation",
    tags: list[str] | None = None,
    concurrency: int = 2,
) -> dict:
    """Import documents from a directory into the knowledge base.

    Args:
        directory: Path to scan for documents.
        source_machine: Machine identifier for provenance.
        recursive: Whether to recurse into subdirectories.
        use_vision: Use gemma3 vision for scanned PDFs and images.
        extensions: File extensions to process (default: all supported).
        category: Category for stored memories.
        tags: Additional tags (always includes "imported", "document").
        concurrency: Max concurrent store operations.

    Returns:
        Summary dict with counts.
    """
    dir_path = Path(directory)
    if not dir_path.is_dir():
        return {"error": f"Directory not found: {directory}"}

    allowed = extensions or ALL_EXTENSIONS
    pattern = "**/*" if recursive else "*"
    files = sorted(
        f for f in dir_path.glob(pattern)
        if f.is_file() and f.suffix.lower() in allowed and not f.name.startswith("~")
    )

    if not files:
        return {"imported": 0, "skipped": 0, "errors": 0, "chunks": 0, "source": directory, "message": "No matching files found"}

    base_tags = ["imported", "document"] + (tags or [])
    sem = asyncio.Semaphore(concurrency)

    imported = 0
    skipped = 0
    errors = 0
    chunks_stored = 0
    vision_used = 0

    for file_path in files:
        ext = file_path.suffix.lower()
        title = file_path.stem.replace("-", " ").replace("_", " ")
        rel_path = str(file_path.relative_to(dir_path))

        try:
            # Extract text based on file type
            text = ""
            used_vision = False

            if ext in TEXT_EXTENSIONS:
                text = _extract_text_file(file_path)

            elif ext in DOCX_EXTENSIONS:
                text = _extract_docx(file_path)

            elif ext in PDF_EXTENSIONS:
                text, has_text = _extract_pdf_text(file_path)
                if not has_text and use_vision:
                    logger.info("PDF %s has no extractable text, using vision", file_path.name)
                    text = await _extract_pdf_vision(file_path)
                    used_vision = True

            elif ext in IMAGE_EXTENSIONS:
                if use_vision:
                    text = await _extract_image_vision(file_path)
                    used_vision = True
                else:
                    skipped += 1
                    continue

            if not text or len(text.strip()) < MIN_CONTENT_CHARS:
                skipped += 1
                continue

            if used_vision:
                vision_used += 1

            # Chunk large documents
            chunks = _chunk_text(text.strip(), title)

            # Determine file-type-specific tags
            file_tags = list(base_tags)
            if ext in PDF_EXTENSIONS:
                file_tags.append("pdf")
            elif ext in DOCX_EXTENSIONS:
                file_tags.append("docx")
            elif ext in IMAGE_EXTENSIONS:
                file_tags.append("image")
            if used_vision:
                file_tags.append("vision-extracted")

            # Store each chunk
            for chunk in chunks:
                async with sem:
                    try:
                        await store_memory_with_extraction(
                            content=chunk,
                            summary=f"Document: {title}"[:200],
                            category=category,
                            tags=file_tags,
                            source_type="document",
                            source_machine=source_machine,
                            source_ref=rel_path,
                            confidence=0.7 if used_vision else 0.8,
                            metadata={
                                "file_path": rel_path,
                                "file_type": ext.lstrip("."),
                                "vision_extracted": used_vision,
                            },
                        )
                        chunks_stored += 1
                    except Exception:
                        logger.exception("Failed to store chunk from %s", file_path.name)
                        errors += 1

            imported += 1

        except Exception:
            logger.exception("Failed to process %s", file_path.name)
            errors += 1

    logger.info(
        "Document import: %d files imported (%d chunks), %d skipped, %d errors, %d vision-extracted from %s",
        imported, chunks_stored, skipped, errors, vision_used, directory,
    )
    return {
        "imported": imported,
        "chunks": chunks_stored,
        "skipped": skipped,
        "errors": errors,
        "vision_extracted": vision_used,
        "source": directory,
    }
