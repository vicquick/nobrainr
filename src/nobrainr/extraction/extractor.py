"""Ollama structured output client for entity extraction."""

import logging

from nobrainr.extraction.llm import ollama_chat
from nobrainr.extraction.models import ExtractionResult

logger = logging.getLogger("nobrainr")

SYSTEM_PROMPT = """\
You are a precise entity and relationship extractor for a knowledge graph. \
Extract only clearly identifiable, specific, named entities and factual relationships.

Entity types: person, project, technology, concept, file, config, error, location, \
organization, service, database, command, container.
Relationship types: uses, depends_on, fixes, part_of, created_by, \
deployed_on, configured_with, replaces, conflicts_with, runs_on, implements.

CRITICAL RULES — violating these pollutes the knowledge graph:
- Extract ONLY proper nouns, named tools, named projects, named technologies, named people.
- NEVER extract generic words as entities: "project", "agent", "server", "configuration", \
"file", "code", "script", "function", "model", "test", "fix", "update", "feature".
- NEVER extract git branch names as entities. Branches like "feature/xyz" or "agent/abc" \
are NOT entities — they are implementation details.
- NEVER extract "Co-Authored-By" AI model names (Claude, GPT, etc.) from commit messages — \
they are metadata, not meaningful entities.
- NEVER hallucinate relationships. Only extract relationships that are EXPLICITLY stated \
or directly implied in the text. If A and B are both mentioned but not related, do NOT \
create a relationship between them.
- NEVER use "relates_to" — if you cannot determine a specific relationship type, skip it.
- Entity names must be specific: "PostgreSQL" not "database", "Vue.js" not "frontend".
- Descriptions should capture what the entity IS, not repeat the text.
- Fewer high-quality entities are MUCH better than many low-quality ones.
- If the text is a simple commit message with no meaningful entities, return empty lists.

Confidence calibration:
- 1.0: explicitly stated fact ("X uses Y", "X depends on Y")
- 0.8: strongly implied ("configured X with Y settings")
- 0.5: inferred but clear from context
- Never go below 0.5 — if you're not confident, don't extract it.

Example input: "Fixed the Docker networking issue by adding nobrainr to the mcp network. \
The pgvector container is now reachable on port 5432."
Example output:
entities: [{name: "Docker", type: "technology", description: "Container runtime"}, \
{name: "nobrainr", type: "service", description: "Memory service"}, \
{name: "mcp", type: "config", description: "Docker network for MCP services"}, \
{name: "pgvector", type: "container", description: "PostgreSQL + pgvector database"}]
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
