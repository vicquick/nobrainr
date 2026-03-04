"""Pydantic models for structured extraction output."""

from typing import Literal

from pydantic import BaseModel, Field


ENTITY_TYPES = Literal[
    "person",
    "project",
    "technology",
    "concept",
    "file",
    "config",
    "error",
    "location",
    "organization",
]

RELATIONSHIP_TYPES = Literal[
    "uses",
    "depends_on",
    "fixes",
    "relates_to",
    "part_of",
    "created_by",
    "deployed_on",
    "configured_with",
]


class ExtractedEntity(BaseModel):
    name: str = Field(description="Entity name (e.g. 'PostgreSQL', 'bimavo', 'John')")
    entity_type: ENTITY_TYPES = Field(description="Type of entity")
    description: str = Field(description="Brief description of the entity in context")


class ExtractedRelationship(BaseModel):
    source: str = Field(description="Source entity name (must match an extracted entity)")
    target: str = Field(description="Target entity name (must match an extracted entity)")
    relationship_type: RELATIONSHIP_TYPES = Field(description="Type of relationship")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence score 0-1")


class ExtractionResult(BaseModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relationships: list[ExtractedRelationship] = Field(default_factory=list)
