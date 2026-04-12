"""Ollama structured output client for entity extraction."""

import logging

from nobrainr.extraction.llm import ollama_chat
from nobrainr.extraction.models import ExtractionResult

logger = logging.getLogger("nobrainr")

SYSTEM_PROMPT = """\
You are a precise entity and relationship extractor for a knowledge graph. \
Extract clearly identifiable, specific, named entities and ALL factual relationships \
between them — both explicit and reasonably implied.

CRITICAL: You MUST respond with a single valid JSON object. All keys MUST be double-quoted strings. \
The response must have exactly two keys: "entities" (array) and "relationships" (array).

Entity entity_types: person, project, technology, concept, file, config, error, location, \
organization, service, database, command, container, package.
Relationship relationship_types: uses, depends_on, fixes, part_of, created_by, \
deployed_on, configured_with, replaces, conflicts_with, runs_on, implements.

ENTITY RULES:
- Extract ONLY proper nouns, named tools, named projects, named technologies, named people.
- NEVER extract generic words: "project", "agent", "server", "configuration", "file", etc.
- NEVER extract git branch names (feature/xyz, agent/abc).
- NEVER extract "Co-Authored-By" AI model names from commit messages.
- Entity names must be specific: "PostgreSQL" not "database", "Vue.js" not "frontend".
- Fewer high-quality entities are MUCH better than many low-quality ones.
- For commit messages: extract project names, feature names, and technologies mentioned.
  "feat: Price Archive, WebSocket live updates" → Price Archive (feature), WebSocket (technology).
- For source code: extract project name, key classes/components, frameworks used.
  Only return empty if the code is a trivial init file or re-export with no meaningful content.

TEMPORAL ANCHORING:
- When dates or timeframes are mentioned, include them in entity descriptions.
- Prefer "what happened" over "what was planned" — extract outcomes, not intentions.
- If both an old and new state are described, capture the transition (e.g. "replaced X with Y").

RELATIONSHIP RULES — this is critical for a connected graph:
- For EVERY pair of extracted entities, actively consider whether a relationship exists.
- Extract relationships that are explicitly stated OR reasonably implied by context.
- If two technologies are used together in a workflow, that IS a relationship (uses, depends_on).
- If a tool is configured with a library, that IS a relationship (configured_with).
- If a service runs inside a container/platform, that IS a relationship (runs_on, deployed_on).
- NEVER use "relates_to" — always pick a specific type or skip.
- Prefer entity↔entity relationships over entity→project hub relationships.

Confidence calibration:
- 1.0: explicitly stated ("X uses Y", "X depends on Y")
- 0.8: strongly implied ("configured X with Y settings", "X and Y used together")
- 0.6: reasonably inferred from shared context
- Never go below 0.5 — if you're not confident, skip it.

Example input: "Fixed the Docker networking issue by adding nobrainr to the mcp network. \
The pgvector container is now reachable on port 5432."
Example output:
{"entities": [{"name": "Docker", "entity_type": "technology", "description": "Container runtime"}, \
{"name": "nobrainr", "entity_type": "service", "description": "Memory and knowledge graph service"}, \
{"name": "mcp", "entity_type": "config", "description": "Docker network for MCP services"}, \
{"name": "pgvector", "entity_type": "container", "description": "PostgreSQL + pgvector database"}], \
"relationships": [{"source": "nobrainr", "target": "mcp", "relationship_type": "deployed_on", "confidence": 1.0}, \
{"source": "pgvector", "target": "mcp", "relationship_type": "deployed_on", "confidence": 1.0}, \
{"source": "nobrainr", "target": "pgvector", "relationship_type": "depends_on", "confidence": 0.8}, \
{"source": "pgvector", "target": "Docker", "relationship_type": "runs_on", "confidence": 0.8}, \
{"source": "nobrainr", "target": "Docker", "relationship_type": "runs_on", "confidence": 0.8}]}

Example input: "Switched from nomic-embed-text to snowflake-arctic-embed2 for embeddings. \
Updated the HNSW indexes in pgvector to 1024 dimensions."
Example output:
{"entities": [{"name": "nomic-embed-text", "entity_type": "technology", "description": "768d embedding model"}, \
{"name": "snowflake-arctic-embed2", "entity_type": "technology", "description": "1024d embedding model"}, \
{"name": "pgvector", "entity_type": "database", "description": "PostgreSQL vector extension"}, \
{"name": "HNSW", "entity_type": "concept", "description": "Approximate nearest neighbor index type"}], \
"relationships": [{"source": "snowflake-arctic-embed2", "target": "nomic-embed-text", "relationship_type": "replaces", "confidence": 1.0}, \
{"source": "pgvector", "target": "HNSW", "relationship_type": "uses", "confidence": 1.0}, \
{"source": "pgvector", "target": "snowflake-arctic-embed2", "relationship_type": "configured_with", "confidence": 0.8}]}\
"""


# Conservative content budget. The old 6000-char limit assumed ~4 chars/token,
# but dense code and non-ASCII content tokenizes at 1-2 chars/token. With
# system_prompt (~250 tok) + known_entities (~500 tok) + structured-output
# schema overhead (~200 tok), the user content budget for num_ctx=8192 is
# approximately 7200 tokens ≈ 4000 chars at the worst-case 1.8 chars/token.
# The 2026-04-12 overnight monitoring loop caught a persistent 400 Bad
# Request storm from llama-server when the old 6000-char limit was in play.
MAX_CONTENT_CHARS = 4000

# Cap each known-entity description to avoid bloating the neighborhood
# context. 15 entities × 120 chars = ~1800 chars ≈ ~500 tokens — safe.
_MAX_ENTITY_DESC_CHARS = 120

# Hard cap on the assembled user prompt (content + neighborhood). If the
# combined text still exceeds this after per-field truncation, we truncate
# the tail. This is the defense-in-depth guard that the old code lacked.
_MAX_USER_PROMPT_CHARS = 5500


async def extract_entities(
    text: str,
    known_entities: list[dict] | None = None,
) -> ExtractionResult:
    """Extract entities and relationships from text using Ollama structured output.

    Args:
        text: Memory content to extract from.
        known_entities: Optional list of nearby entities from the graph
            (each dict has name, entity_type, description) to help the LLM
            link to existing nodes rather than creating duplicates.
    """
    # Truncate long content to avoid exceeding context window
    if len(text) > MAX_CONTENT_CHARS:
        text = text[:MAX_CONTENT_CHARS] + "…"

    # Build user prompt with optional neighborhood context
    user_parts = []
    if known_entities:
        entity_lines = []
        for e in known_entities[:15]:  # cap count to avoid blowing context
            desc = (e.get("description") or "")[:_MAX_ENTITY_DESC_CHARS]
            entity_lines.append(f"  - {e['name']} ({e['entity_type']}){': ' + desc if desc else ''}")
        user_parts.append(
            "Known entities already in the graph (reuse these names if they appear):\n"
            + "\n".join(entity_lines)
            + "\n"
        )
    user_parts.append(f"Extract entities and relationships from this memory:\n\n{text}")
    user_prompt = "\n".join(user_parts)

    # Defense-in-depth: if the assembled prompt still exceeds the hard cap
    # (e.g. because known_entities had very long names), truncate the tail.
    if len(user_prompt) > _MAX_USER_PROMPT_CHARS:
        user_prompt = user_prompt[:_MAX_USER_PROMPT_CHARS] + "\n…[truncated]"

    try:
        parsed = await ollama_chat(
            system=SYSTEM_PROMPT,
            user=user_prompt,
            schema=ExtractionResult.model_json_schema(),
            keep_alive="24h",
            think=False,
        )
        return ExtractionResult.model_validate(parsed)

    except Exception:
        logger.exception("Entity extraction failed")
        raise
