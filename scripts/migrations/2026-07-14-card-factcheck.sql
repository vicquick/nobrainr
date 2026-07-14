-- M1 (HEART PLAN): cards get a published_accuracy number.
-- card_factcheck job extracts checkable claims per card, verifies them
-- (mechanically via verification_probes results, else LLM vs newest
-- evidence), and stamps the score. Cards below card_min_accuracy get
-- source_max_updated reset so card_builder rebuilds them with the
-- refuted claims injected as "do not restate".
ALTER TABLE context_cards ADD COLUMN IF NOT EXISTS published_accuracy real;
ALTER TABLE context_cards ADD COLUMN IF NOT EXISTS factcheck jsonb;
ALTER TABLE context_cards ADD COLUMN IF NOT EXISTS factchecked_at timestamptz;
