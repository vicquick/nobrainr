"""Export the commonbook canon for voice-LoRA training (C3 prep, 2026-07-14).

Stages — does NOT train. Pulls the author's own writing (creative claim
kind + affine/docx/markdown sources + personal tags) from nobrainr into a
JSONL corpus suitable for a style LoRA. The research finding this serves
(arxiv 2601.18353): expert preference for human writing flips 82.7% → 62%
pro-AI *after fine-tuning on the author's complete works* — so completeness
and voice-purity of this export are what make an expert-preferred twin
possible. Knowledge stays in nobrainr (RAG); this corpus is for VOICE only.

Two record shapes, mixed per the April pilot plan (70% completion / 30% QA):
  completion : {"text": "<the writing>"}                  — style capture
  qa         : {"prompt": "<title/topic>", "completion": "<the writing>"}

Excludes: system/agent/crawl/github/chatgpt sources (not the author's
voice), superseded rows, and anything below a length floor.

Usage (inside the nobrainr container):
    docker exec <nobrainr> python3 /app/scripts/export_voice_corpus.py \
        --out /tmp/voice_corpus.jsonl [--min-len 120]
Then copy off-container. The .jsonl and any weights derived from it are
PRIVATE artifacts — local-only, never published.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

for c in [Path(__file__).resolve().parents[1] / "src", Path("/app/src"), Path("/app")]:
    if c.exists() and str(c) not in sys.path:
        sys.path.insert(0, str(c))
        break

from nobrainr.db.pool import get_pool  # noqa: E402

CORPUS_SQL = """
    SELECT id, content, summary, tags, source_type, claim_kind
    FROM memories
    WHERE superseded_by IS NULL
      AND length(content) >= $1
      AND (
            claim_kind = 'creative'
         OR source_type IN ('affine_memos', 'docx', 'markdown_notes')
         OR tags && ARRAY['poetry','poem','idea','ideas','reflection',
                          'philosophy','aphorism','personal-goal','commonbook']
      )
      AND source_type NOT IN ('github','crawl','chatgpt','session','agent',
                              'agent_learning','system')
    ORDER BY created_at
"""


async def main(out: str, min_len: int, qa_ratio: float) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(CORPUS_SQL, min_len)

    n_completion = n_qa = 0
    with open(out, "w", encoding="utf-8") as f:
        for i, r in enumerate(rows):
            text = (r["content"] or "").strip()
            if not text:
                continue
            # Deterministic split (no RNG in this env): every Nth row is QA.
            is_qa = r["summary"] and (i % max(1, round(1 / max(qa_ratio, 0.01))) == 0)
            if is_qa:
                rec = {"prompt": r["summary"].strip()[:200], "completion": text}
                n_qa += 1
            else:
                rec = {"text": text}
                n_completion += 1
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    total = n_completion + n_qa
    chars = sum(len(r["content"] or "") for r in rows)
    print(json.dumps({
        "out": out, "records": total,
        "completion": n_completion, "qa": n_qa,
        "approx_chars": chars, "approx_tokens": chars // 4,
        "note": "PRIVATE — local-only, never publish weights derived from this",
    }, indent=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/voice_corpus.jsonl")
    ap.add_argument("--min-len", type=int, default=120)
    ap.add_argument("--qa-ratio", type=float, default=0.30)
    args = ap.parse_args()
    asyncio.run(main(args.out, args.min_len, args.qa_ratio))
