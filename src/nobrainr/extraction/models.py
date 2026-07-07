"""Pydantic models for structured extraction output."""

from pydantic import BaseModel, Field

# Known types for reference — not enforced as Literal to allow LLM creativity
ENTITY_TYPES = [
    "person", "project", "technology", "concept", "file", "config",
    "error", "location", "organization", "service", "database",
    "command", "port", "container", "package",
]

# Closed predicate vocabulary (P2a, 2026-07-07). The old "not enforced to
# allow LLM creativity" stance produced 2,389 distinct relationship types
# in production (251k rows on the canonical 11, a 12k-row freeform tail of
# inverses, synonyms and one-offs) and a relation F1 of 0.03 — the Zep
# paper's closed-vocabulary discipline is the fix. v2 adds `causes` and
# `tests` (organically frequent, semantically distinct); `relates_to`
# stays banned. The list is injected as a JSON-schema enum on the field
# below, so llama.cpp grammar-constrained decoding enforces it at the
# token level (the cooccurrence job proved this works).
CANONICAL_RELATIONSHIP_TYPES = [
    "uses", "depends_on", "fixes", "part_of", "created_by",
    "deployed_on", "configured_with", "replaces", "conflicts_with",
    "runs_on", "implements", "causes", "tests",
]

# Back-compat alias (older code imports RELATIONSHIP_TYPES).
RELATIONSHIP_TYPES = CANONICAL_RELATIONSHIP_TYPES

# Freeform → (canonical, swap_source_target). Inverses swap the edge
# direction: used_by(A, B) == uses(B, A). Only unambiguous mappings live
# here — anything unmapped and non-canonical is dropped (a closed
# vocabulary refuses junk instead of laundering it).
_REL_SYNONYMS: dict[str, tuple[str, bool] | None] = {
    "relates_to": None,          # banned — carries no information
    "related_to": None,
    "used_by": ("uses", True),
    "used_in": ("uses", True),
    "relies_on": ("depends_on", False),
    "requires": ("depends_on", False),
    "required_by": ("depends_on", True),
    "implemented_by": ("implements", True),
    "implemented_in": ("implements", True),
    "caused_by": ("causes", True),
    "creates": ("created_by", True),
    "contains": ("part_of", True),
    "includes": ("part_of", True),
    "member_of": ("part_of", False),
    "belongs_to": ("part_of", False),
    "configures": ("configured_with", True),
    "supports": ("uses", True),
    "handles": ("uses", False),
    "tested_by": ("tests", True),
    "replaced_by": ("replaces", True),
    "superseded_by": ("replaces", True),
    "supersedes": ("replaces", False),
    "deprecates": ("replaces", False),
    "deprecated_in": ("replaces", True),
    "runs": ("runs_on", True),
    "hosts": ("deployed_on", True),
    "hosted_on": ("deployed_on", False),
    "deployed_to": ("deployed_on", False),
    "fixed_by": ("fixes", True),
    "resolves": ("fixes", False),
    "solved_by": ("fixes", True),
    "conflicts": ("conflicts_with", False),
    "integrates_with": ("uses", False),
    "connects_to": ("uses", False),
}

_CANONICAL_SET = set(CANONICAL_RELATIONSHIP_TYPES)
_UNMAPPED = object()


def normalize_relationship(
    rel_type: str, source: str, target: str,
) -> tuple[str, str, str] | None:
    """Map a freeform relationship onto the closed vocabulary.

    Returns (canonical_type, source, target) — source/target swapped for
    inverse predicates — or None when the relation can't be expressed in
    the vocabulary (caller drops it).
    """
    key = (rel_type or "").strip().lower().replace(" ", "_").replace("-", "_")
    if key in _CANONICAL_SET:
        return key, source, target
    mapped = _REL_SYNONYMS.get(key, _UNMAPPED)
    if mapped is None or mapped is _UNMAPPED:
        return None
    canonical, swap = mapped
    return (canonical, target, source) if swap else (canonical, source, target)


class ExtractedEntity(BaseModel):
    name: str = Field(description="Entity name (e.g. 'PostgreSQL', 'Docker', 'nginx')")
    entity_type: str = Field(description="Type of entity")
    description: str = Field(default="", description="Brief description of the entity in context")


class ExtractedRelationship(BaseModel):
    source: str = Field(description="Source entity name (must match an extracted entity)")
    target: str = Field(description="Target entity name (must match an extracted entity)")
    relationship_type: str = Field(
        description="Type of relationship",
        json_schema_extra={"enum": CANONICAL_RELATIONSHIP_TYPES},
    )
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Confidence score 0-1")


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relationships: list[ExtractedRelationship] = Field(default_factory=list)
