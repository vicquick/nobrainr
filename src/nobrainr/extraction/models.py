"""Pydantic models for structured extraction output."""

from pydantic import BaseModel, Field


# Known types for reference — not enforced as Literal to allow LLM creativity
ENTITY_TYPES = [
    "person", "project", "technology", "concept", "file", "config",
    "error", "location", "organization", "service", "database",
    "command", "port", "container", "package",
]

RELATIONSHIP_TYPES = [
    "uses", "depends_on", "fixes", "relates_to", "part_of", "created_by",
    "deployed_on", "configured_with", "replaces", "conflicts_with",
    "runs_on", "implements",
]


class ExtractedEntity(BaseModel):
    name: str = Field(description="Entity name (e.g. 'PostgreSQL', 'Docker', 'nginx')")
    entity_type: str = Field(description="Type of entity")
    description: str = Field(default="", description="Brief description of the entity in context")


class ExtractedRelationship(BaseModel):
    source: str = Field(description="Source entity name (must match an extracted entity)")
    target: str = Field(description="Target entity name (must match an extracted entity)")
    relationship_type: str = Field(description="Type of relationship")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Confidence score 0-1")


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relationships: list[ExtractedRelationship] = Field(default_factory=list)
