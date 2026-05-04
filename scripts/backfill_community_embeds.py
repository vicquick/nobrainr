#!/usr/bin/env python3
"""One-shot backfill of community_summaries.embedding from title + summary."""
import asyncio, sys
sys.path.insert(0, '/app/src')
from nobrainr.db.pool import get_pool
from nobrainr.embeddings.ollama import embed_text

async def main():
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT community_id, title, summary FROM community_summaries WHERE embedding IS NULL"
        )
    print(f"backfilling {len(rows)} community summaries")
    n = 0
    async with pool.acquire() as conn:
        for r in rows:
            text = (r["title"] or "") + " | " + (r["summary"] or "")
            text = text[:2000].strip()
            if not text:
                continue
            try:
                emb = await embed_text(text)
                await conn.execute(
                    "UPDATE community_summaries SET embedding=$1::vector, embedding_model='qwen3-embedding:0.6b' WHERE community_id=$2",
                    emb, r["community_id"],
                )
                n += 1
                if n % 25 == 0:
                    print(f"  {n}/{len(rows)}")
            except Exception as exc:
                print(f"  ! community {r['community_id']}: {exc}")
    print(f"done: {n} embedded")

if __name__ == "__main__":
    asyncio.run(main())
