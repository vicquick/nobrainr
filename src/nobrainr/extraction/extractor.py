"""Ollama structured output client for entity extraction."""

import logging

from nobrainr.extraction.llm import ollama_chat
from nobrainr.extraction.models import ExtractionResult

logger = logging.getLogger("nobrainr")

SYSTEM_PROMPT = """\
You are an entity and relationship extractor. Given a memory or knowledge entry, \
extract all notable entities and the relationships between them.

Entity types: person, project, technology, concept, file, config, error, location, organization.
Relationship types: uses, depends_on, fixes, relates_to, part_of, created_by, deployed_on, configured_with.

Rules:
- Only extract entities that are clearly identifiable from the text.
- Entity names should be concise (e.g. "PostgreSQL" not "the PostgreSQL database").
- Each relationship source and target must match an extracted entity name exactly.
- Assign confidence scores (0-1) to relationships based on how explicitly stated they are.
- If there are no entities or relationships, return empty lists.\
"""


async def extract_entities(text: str) -> ExtractionResult:
    """Extract entities and relationships from text using Ollama structured output."""
    try:
        parsed = await ollama_chat(
            system=SYSTEM_PROMPT,
            user=f"Extract entities and relationships from this memory:\n\n{text}",
            schema=ExtractionResult.model_json_schema(),
            keep_alive="5m",
        )
        return ExtractionResult.model_validate(parsed)

    except Exception:
        logger.exception("Entity extraction failed")
        raise
