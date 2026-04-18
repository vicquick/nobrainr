"""One-shot tag canonicalization backfill.

Rewrites `memories.tags` using utils.tags.canonicalize_tags so the DB
matches what the runtime normalizer produces from now on. Idempotent —
re-running is a no-op once converged.

Usage (from inside the nobrainr container):
    python3 /app/scripts/migrations/2026-04-18-canonicalize-tags.py [--dry-run]

Or against the live DB via docker exec:
    docker cp scripts/migrations/2026-04-18-canonicalize-tags.py \
        <container>:/tmp/canon-tags.py
    docker exec <container> python3 /tmp/canon-tags.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow running as a script from the repo root OR from inside the container
# where it lives at /tmp/canon-tags.py and the src tree is already on sys.path.
_here = Path(__file__).resolve()
_candidates: list[Path] = []
try:
    _candidates.append(_here.parents[2] / "src")
except IndexError:
    pass
_candidates.extend([Path("/app/src"), Path("/app")])
for candidate in _candidates:
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))
        break

from nobrainr.db.pool import get_pool  # noqa: E402
from nobrainr.utils.tags import canonicalize_tags  # noqa: E402


BATCH_SIZE = 2000


async def run(dry_run: bool) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM memories")
        print(f"scanning {total:,} memories for tag drift")

        changed = 0
        scanned = 0
        offset = 0

        while True:
            rows = await conn.fetch(
                """
                SELECT id, tags FROM memories
                ORDER BY id
                OFFSET $1 LIMIT $2
                """,
                offset,
                BATCH_SIZE,
            )
            if not rows:
                break

            # Batch the diffs so we write once per BATCH_SIZE.
            pending: list[tuple[list[str], str]] = []
            for r in rows:
                old = list(r["tags"] or [])
                new = canonicalize_tags(old)
                if old != new:
                    pending.append((new, str(r["id"])))
            scanned += len(rows)

            if pending and not dry_run:
                async with conn.transaction():
                    # Tag the version rows so this mass-update is
                    # distinguishable from real edits in audit history.
                    await conn.execute(
                        "SELECT set_config('nobrainr.change_type', 'tag_canonicalize', true)"
                    )
                    await conn.execute(
                        "SELECT set_config('nobrainr.changed_by', 'migration:2026-04-18', true)"
                    )
                    await conn.execute(
                        "SELECT set_config('nobrainr.change_reason', 'canonicalize-tags', true)"
                    )
                    for new_tags, mid in pending:
                        await conn.execute(
                            "UPDATE memories SET tags = $1 WHERE id = $2::uuid",
                            new_tags,
                            mid,
                        )
            changed += len(pending)
            print(
                f"  batch offset={offset:,} rows={len(rows)} "
                f"changed={len(pending)} (running total changed={changed:,})"
            )
            offset += BATCH_SIZE

        before_unique = await conn.fetchval(
            "SELECT COUNT(DISTINCT t) FROM (SELECT unnest(tags) t FROM memories) x"
        )
        print(
            f"\nscanned={scanned:,}  changed={changed:,}  "
            f"unique_tags_after={before_unique:,}  dry_run={dry_run}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't write — just count")
    args = ap.parse_args()
    asyncio.run(run(args.dry_run))


if __name__ == "__main__":
    main()
