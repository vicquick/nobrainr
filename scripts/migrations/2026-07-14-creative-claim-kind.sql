-- C4 commonbook (2026-07-14): 'creative' claim_kind for personal
-- writings — poetry, ideas, formulations, goals, reflections. Its
-- staleness model is NONE: a poem is never "outdated", an idea's value
-- is that it exists. Exempt from disuse-decay, probes, and the
-- reconciliation sweep (a later thought does not "supersede" an earlier
-- one — both are the commonbook). This is the corpus whose density
-- makes expert-preferred voice generation possible (arxiv 2601.18353:
-- expert preference flips 82.7%→62% pro-AI after fine-tuning on an
-- author's complete works).
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_claim_kind_check;
ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_claim_kind_chk;  -- stale original from trust-layer migration
ALTER TABLE memories ADD CONSTRAINT memories_claim_kind_check CHECK (
    claim_kind IS NULL OR claim_kind = ANY (ARRAY[
        'code-state', 'infra-state', 'preference', 'incident-fix',
        'design-decision', 'historical', 'reference', 'fact', 'plan',
        'creative'
    ])
);
