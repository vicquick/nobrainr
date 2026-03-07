"""Ollama structured output client for entity extraction."""

import logging

from nobrainr.extraction.llm import ollama_chat
from nobrainr.extraction.models import ExtractionResult

logger = logging.getLogger("nobrainr")

SYSTEM_PROMPT = """\
You are a selective entity and relationship extractor. Given a memory or knowledge entry, \
extract only the MOST IMPORTANT entities and relationships — the ones someone would actually search for later.

Entity types: person, project, technology, concept, file, config, error, location, organization.
Relationship types: uses, depends_on, fixes, relates_to, part_of, created_by, deployed_on, configured_with.

Rules:
- Be SELECTIVE: extract at most 3-5 entities. Only extract specific, named things — not generic concepts.
- SKIP generic terms like "server", "database", "API", "configuration", "error handling", "deployment".
- SKIP vague concepts. "Docker" is good, "containerization" is too generic. "PostgreSQL" is good, "database management" is not.
- Entity names should be concise (e.g. "PostgreSQL" not "the PostgreSQL database").
- Each relationship source and target must match an extracted entity name exactly.
- Only create relationships that represent specific, meaningful connections — not obvious/trivial ones.
- Assign confidence scores (0-1) to relationships based on how explicitly stated they are.
- If nothing specific or noteworthy is mentioned, return empty lists. Most memories should have 0-3 entities.\
"""


async def extract_entities(text: str) -> ExtractionResult:
    """Extract entities and relationships from text using Ollama structured output."""
    try:
        parsed = await ollama_chat(
            system=SYSTEM_PROMPT,
            user=f"Extract entities and relationships from this memory:\n\n{text}",
            schema=ExtractionResult.model_json_schema(),
            keep_alive="24h",
            think=False,
        )
        return ExtractionResult.model_validate(parsed)

    except Exception:
        logger.exception("Entity extraction failed")
        raise
