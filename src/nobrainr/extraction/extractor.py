"""Ollama structured output client for entity extraction."""

import logging

from nobrainr.extraction.llm import ollama_chat
from nobrainr.extraction.models import ExtractionResult

logger = logging.getLogger("nobrainr")

SYSTEM_PROMPT = """\
You are an entity and relationship extractor. Given a memory or knowledge entry, \
extract all notable entities and the relationships between them.

Entity types: person, project, technology, concept, file, config, error, location, \
organization, service, database, command, port, container.
Relationship types: uses, depends_on, fixes, relates_to, part_of, created_by, \
deployed_on, configured_with, replaces, conflicts_with, runs_on, implements.

Rules:
- Only extract entities that are clearly identifiable from the text.
- Entity names should be concise (e.g. "PostgreSQL" not "the PostgreSQL database").
- Each relationship source and target must match an extracted entity name exactly.
- If there are no entities or relationships, return empty lists.

Confidence calibration:
- 1.0: explicitly stated fact ("X uses Y")
- 0.8: strongly implied ("configured X with Y settings")
- 0.5: inferred from context (mentioned together, likely related)
- 0.3: weak/uncertain association

Example input: "Fixed the Docker networking issue by adding nobrainr to the mcp network. \
The pgvector container is now reachable on port 5432."
Example output:
entities: [{name: "Docker", type: "technology", description: "Container runtime"}, \
{name: "nobrainr", type: "service", description: "Memory service that was reconnected"}, \
{name: "mcp", type: "config", description: "Docker network for MCP services"}, \
{name: "pgvector", type: "container", description: "PostgreSQL + pgvector database container"}, \
{name: "5432", type: "port", description: "PostgreSQL listening port"}]
relationships: [{source: "nobrainr", target: "mcp", type: "deployed_on", confidence: 1.0}, \
{source: "pgvector", target: "mcp", type: "deployed_on", confidence: 1.0}, \
{source: "nobrainr", target: "pgvector", type: "depends_on", confidence: 0.8}]\
"""


MAX_CONTENT_CHARS = 3000  # ~750 tokens — fits comfortably in num_ctx=4096 with prompt


async def extract_entities(text: str) -> ExtractionResult:
    """Extract entities and relationships from text using Ollama structured output."""
    # Truncate long content to avoid exceeding context window
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + "…"
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
