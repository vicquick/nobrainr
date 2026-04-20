"""Decision memory type (ADR-style) for nobrainr.

Why this exists (and why it's distinct from a "learning"):

A **learning** is an observation — something we DISCOVERED through running,
debugging, or measuring. Descriptive. Example: "BGE-v2-m3 CPU is ~1s/doc on
i5-13500 real-memory texts."

A **decision** is a CHOICE — we deliberately picked path A over path B, and
the rationale + rejected alternatives are the valuable part. Prescriptive.
Example: "Keep BGE reranker on CPU with cap=8 instead of moving to GPU
because Qwen3.6-35B reserves all 20GB VRAM at 32K ctx."

Agents querying for "how should I do X?" want decisions. Agents debugging a
bug want learnings. Separating them prevents future-you from re-arguing
settled choices because the "why" rotted inside a long free-form note.

The storage shape piggybacks on `memories.metadata` (jsonb) — no schema
migration needed. The `category='decision'` convention + this Pydantic
validator is the contract. Dedicated MCP tools `decision_store` /
`decision_search` give agents a clean interface.

Pattern borrowed from DecisionNode (github.com/decisionnode/DecisionNode),
adapted for our shared Postgres KB rather than per-project JSON.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DecisionStatus = Literal["active", "deprecated", "superseded"]


class DecisionMetadata(BaseModel):
    """Structured metadata for a `category='decision'` memory.

    Lives inside `memories.metadata` (jsonb). Validated on MCP store so
    required fields are present — without rationale + constraints a
    "decision" memory is indistinguishable from a free-form note.
    """

    scope: str = Field(
        description=(
            "Dotted path identifying the subsystem this decision applies to. "
            "e.g. 'nobrainr/retrieval', 'bimavo/gaeb-parser', 'infra/coolify'."
        ),
        min_length=2, max_length=120,
    )
    decision: str = Field(
        description=(
            "One-sentence imperative statement of what was chosen. "
            "e.g. 'Cap BGE reranker at 8 candidates instead of 150.'"
        ),
        min_length=5, max_length=500,
    )
    rationale: str = Field(
        description=(
            "Why this path — the costs/benefits that made this the right "
            "choice. At least one concrete reason, not just 'seemed good'."
        ),
        min_length=10, max_length=2000,
    )
    constraints: list[str] = Field(
        default_factory=list,
        description=(
            "Hard constraints that rule out the alternatives. "
            "e.g. ['20GB VRAM total', 'Qwen3.6 must keep 32K ctx']."
        ),
    )
    alternatives_rejected: list[str] = Field(
        default_factory=list,
        description=(
            "Paths we considered and explicitly chose not to take, with "
            "a short reason each. Prevents re-debating settled choices. "
            "e.g. ['Jina v2 reranker — TEI Candle rejects trust_remote_code']."
        ),
    )
    supersedes: list[str] = Field(
        default_factory=list,
        description=(
            "Memory-IDs of prior decisions this one replaces. The old "
            "decision should be set status='superseded' by caller."
        ),
    )
    status: DecisionStatus = Field(
        default="active",
        description=(
            "'active' = current policy, search should surface it. "
            "'deprecated' = intentionally no longer followed. "
            "'superseded' = replaced by a newer decision (see supersedes)."
        ),
    )

    def to_dict(self) -> dict:
        return self.model_dump(mode="json", exclude_none=False)
