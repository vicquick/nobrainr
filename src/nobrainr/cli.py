"""nobrainr CLI — manage the memory service."""

import asyncio
import json
import sys

import click
from rich.console import Console
from rich.table import Table

console = Console()


@click.group()
def main():
    """nobrainr — Collective agent memory service."""
    pass


@main.command()
def serve():
    """Start the MCP server with dashboard (ASGI app via uvicorn)."""
    from nobrainr.mcp.server import main as server_main
    console.print("[bold green]Starting nobrainr server...[/]")
    server_main()


@main.command()
def status():
    """Check database, models, and knowledge graph status."""
    async def _status():
        from nobrainr.db.pool import get_pool, close_pool
        from nobrainr.db.schema import init_schema
        from nobrainr.embeddings.ollama import check_model
        from nobrainr.db import queries
        from nobrainr.config import settings

        try:
            pool = await get_pool()
            await init_schema(pool)
            stats = await queries.get_stats()
            model_ok = await check_model()

            # Check extraction model
            import httpx
            extraction_ok = False
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.get(f"{settings.ollama_url}/api/tags")
                    models = resp.json().get("models", [])
                    extraction_ok = any(
                        m["name"].startswith(settings.extraction_model) for m in models
                    )
            except Exception:
                pass

            await close_pool()
            return stats, model_ok, extraction_ok
        except Exception as e:
            return None, str(e), False

    stats, model_ok, extraction_ok = asyncio.run(_status())

    if stats is None:
        console.print(f"[bold red]Database connection failed:[/] {model_ok}")
        sys.exit(1)

    table = Table(title="nobrainr Status")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Total memories", str(stats["total_memories"]))
    table.add_row("Raw conversations", str(stats["raw_conversations"]))
    table.add_row("Entities", str(stats.get("total_entities", 0)))
    table.add_row("Relations", str(stats.get("total_relations", 0)))
    table.add_row("Extracted", str(stats.get("extraction_done", 0)))
    table.add_row("Pending extraction", str(stats.get("extraction_pending", 0)))
    table.add_row("Embedding model", "[green]OK[/]" if model_ok is True else "[red]NOT FOUND[/]")
    table.add_row("Extraction model", "[green]OK[/]" if extraction_ok else "[red]NOT FOUND[/]")

    for src in stats.get("by_source", []):
        table.add_row(f"  Source: {src['source_type']}", str(src["cnt"]))
    for cat in stats.get("by_category", []):
        table.add_row(f"  Category: {cat['category']}", str(cat["cnt"]))
    for machine in stats.get("by_machine", []):
        table.add_row(f"  Machine: {machine['source_machine']}", str(machine["cnt"]))

    console.print(table)

    if stats.get("top_tags"):
        tag_table = Table(title="Top Tags")
        tag_table.add_column("Tag", style="cyan")
        tag_table.add_column("Count", style="green")
        for tag in stats["top_tags"][:10]:
            tag_table.add_row(tag["tag"], str(tag["cnt"]))
        console.print(tag_table)


@main.command()
@click.argument("query")
@click.option("--limit", "-l", default=10, help="Max results")
@click.option("--threshold", "-t", default=0.3, help="Min similarity")
@click.option("--tags", help="Comma-separated tags to filter")
@click.option("--category", "-c", help="Filter by category")
def search(query, limit, threshold, tags, category):
    """Semantic search across memories."""
    async def _search():
        from nobrainr.db.pool import get_pool, close_pool
        from nobrainr.db.schema import init_schema
        from nobrainr.db import queries as q
        from nobrainr.embeddings.ollama import embed_text

        pool = await get_pool()
        await init_schema(pool)
        embedding = await embed_text(query)
        tag_list = [t.strip() for t in tags.split(",")] if tags else None
        results = await q.search_memories(
            embedding=embedding, limit=limit, threshold=threshold,
            tags=tag_list, category=category,
        )
        await close_pool()
        return results

    results = asyncio.run(_search())

    if not results:
        console.print("[yellow]No memories found.[/]")
        return

    for i, mem in enumerate(results, 1):
        sim = mem.get("similarity", 0)
        rel = mem.get("relevance", 0)
        console.print(f"\n[bold cyan]#{i}[/] [dim](sim: {sim:.4f} | rel: {rel:.4f})[/]")
        if mem.get("summary"):
            console.print(f"  [bold]{mem['summary']}[/]")
        console.print(f"  {mem['content'][:200]}...")
        tags_str = ", ".join(mem.get("tags", []))
        if tags_str:
            console.print(f"  [dim]Tags: {tags_str}[/]")


@main.command("extract-backfill")
@click.option("--batch-size", "-b", default=5, help="Memories per batch")
def extract_backfill(batch_size):
    """Run entity extraction on all unprocessed memories."""
    async def _backfill():
        from nobrainr.db.pool import get_pool, close_pool
        from nobrainr.db.schema import init_schema
        from nobrainr.extraction.pipeline import backfill

        pool = await get_pool()
        await init_schema(pool)

        def on_progress(count, memory):
            summary = memory.get("summary") or memory.get("content", "")[:60]
            console.print(f"  [green]#{count}[/] {summary}")

        total = await backfill(batch_size=batch_size, on_progress=on_progress)
        await close_pool()
        return total

    console.print("[bold]Starting entity extraction backfill...[/]")
    total = asyncio.run(_backfill())
    console.print(f"\n[bold green]Done![/] Processed {total} memories.")


@main.command()
@click.option("--type", "-t", "entity_type", help="Filter by entity type")
@click.option("--limit", "-l", default=50, help="Max results")
def entities(entity_type, limit):
    """List extracted entities from the knowledge graph."""
    async def _entities():
        from nobrainr.db.pool import get_pool, close_pool
        from nobrainr.db.schema import init_schema
        from nobrainr.db import queries as q

        pool = await get_pool()
        await init_schema(pool)
        results = await q.list_entities(entity_type=entity_type, limit=limit)
        await close_pool()
        return results

    results = asyncio.run(_entities())

    if not results:
        console.print("[yellow]No entities found.[/]")
        return

    table = Table(title=f"Entities ({len(results)})")
    table.add_column("Name", style="cyan")
    table.add_column("Type", style="green")
    table.add_column("Mentions", style="yellow", justify="right")
    table.add_column("Description", max_width=50)

    for e in results:
        table.add_row(
            e["name"],
            e["entity_type"],
            str(e.get("mention_count", 0)),
            (e.get("description") or "")[:50],
        )

    console.print(table)


@main.command("import-chatgpt")
@click.argument("file_path")
@click.option("--distill/--no-distill", default=False, help="Extract learnings into memories")
def import_chatgpt(file_path, distill):
    """Import ChatGPT conversations from export JSON."""
    async def _import():
        from nobrainr.db.pool import get_pool, close_pool
        from nobrainr.db.schema import init_schema
        from nobrainr.importers.chatgpt import import_chatgpt_export

        pool = await get_pool()
        await init_schema(pool)
        result = await import_chatgpt_export(file_path, distill=distill)
        await close_pool()
        return result

    with console.status("Importing ChatGPT conversations..."):
        result = asyncio.run(_import())

    console.print_json(json.dumps(result, indent=2))


@main.command("import-claude")
@click.argument("directory")
@click.option("--machine", "-m", help="Machine name (e.g. 'my-server', 'laptop')")
def import_claude(directory, machine):
    """Import Claude memory files from .claude directory."""
    async def _import():
        from nobrainr.db.pool import get_pool, close_pool
        from nobrainr.db.schema import init_schema
        from nobrainr.importers.claude import import_claude_memory

        pool = await get_pool()
        await init_schema(pool)
        result = await import_claude_memory(directory, machine_name=machine)
        await close_pool()
        return result

    with console.status("Importing Claude memory files..."):
        result = asyncio.run(_import())

    console.print_json(json.dumps(result, indent=2))


@main.command("import-documents")
@click.argument("directory")
@click.option("--machine", "-m", help="Machine name for provenance")
@click.option("--category", "-c", default="documentation", help="Category for stored memories")
@click.option("--tags", help="Comma-separated extra tags")
@click.option("--no-vision", is_flag=True, help="Skip vision extraction for images/scanned PDFs")
@click.option("--no-recursive", is_flag=True, help="Don't recurse into subdirectories")
def import_documents(directory, machine, category, tags, no_vision, no_recursive):
    """Import documents (PDF, images, DOCX, markdown, text) from a directory."""
    async def _import():
        from nobrainr.db.pool import get_pool, close_pool
        from nobrainr.db.schema import init_schema
        from nobrainr.importers.documents import import_documents as do_import

        pool = await get_pool()
        await init_schema(pool)
        tag_list = [t.strip() for t in tags.split(",")] if tags else None
        result = await do_import(
            directory,
            source_machine=machine,
            use_vision=not no_vision,
            category=category,
            tags=tag_list,
            recursive=not no_recursive,
        )
        await close_pool()
        return result

    with console.status("Importing documents..."):
        result = asyncio.run(_import())

    console.print_json(json.dumps(result, indent=2))


@main.command("re-embed")
@click.option("--model", "-m", default=None, help="Embedding model (default: from config)")
@click.option("--dimensions", "-d", type=int, default=None, help="Vector dimensions (default: from config)")
@click.option("--batch-size", "-b", default=32, help="Texts per embedding batch")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def re_embed(model, dimensions, batch_size, yes):
    """Migrate to a new embedding model. Re-embeds all memories and entities."""
    from nobrainr.config import settings

    embed_model = model or settings.embedding_model
    embed_dims = dimensions or settings.embedding_dimensions

    async def _run():
        import numpy as np
        import httpx
        from nobrainr.db.pool import get_pool, close_pool

        pool = await get_pool()

        # Count records
        async with pool.acquire() as conn:
            mem_count = await conn.fetchval("SELECT count(*) FROM memories")
            ent_count = await conn.fetchval("SELECT count(*) FROM entities")

        console.print("\n[bold]Embedding Migration[/]")
        console.print(f"  Model:      [cyan]{embed_model}[/]")
        console.print(f"  Dimensions: [cyan]{embed_dims}[/]")
        console.print(f"  Memories:   [yellow]{mem_count}[/]")
        console.print(f"  Entities:   [yellow]{ent_count}[/]")

        if not yes:
            click.confirm(
                f"\nThis will re-embed {mem_count} memories and {ent_count} entities. Continue?",
                abort=True,
            )

        async with pool.acquire() as conn:
            # 1. Drop HNSW indexes (can't alter vector dimensions with index)
            console.print("\n[bold]1/6[/] Dropping HNSW indexes...")
            await conn.execute("DROP INDEX IF EXISTS idx_memories_embedding_hnsw")
            await conn.execute("DROP INDEX IF EXISTS idx_memories_embedding_halfvec_hnsw")
            await conn.execute("DROP INDEX IF EXISTS idx_entities_embedding_hnsw")
            await conn.execute("DROP INDEX IF EXISTS idx_entities_embedding_halfvec_hnsw")

            # 2. NULL out embeddings so ALTER TYPE succeeds
            console.print("[bold]2/6[/] Clearing old embeddings...")
            await conn.execute("UPDATE memories SET embedding = NULL")
            await conn.execute("UPDATE entities SET embedding = NULL")

            # 3. ALTER vector columns to new dimensions
            console.print(f"[bold]3/6[/] Altering vector columns to {embed_dims}d...")
            await conn.execute(
                f"ALTER TABLE memories ALTER COLUMN embedding TYPE vector({embed_dims})"
            )
            await conn.execute(
                f"ALTER TABLE entities ALTER COLUMN embedding TYPE vector({embed_dims})"
            )

            # 4. Recreate memory_relevance function with new dimensions
            # Drop all overloads (different vector dimensions create separate functions)
            await conn.execute("""
                DO $$ DECLARE r RECORD;
                BEGIN
                    FOR r IN SELECT oid::regprocedure::text AS sig
                             FROM pg_proc WHERE proname = 'memory_relevance'
                    LOOP
                        EXECUTE 'DROP FUNCTION IF EXISTS ' || r.sig || ' CASCADE';
                    END LOOP;
                END $$;
            """)
            await conn.execute(f"""
                CREATE OR REPLACE FUNCTION memory_relevance(
                    query_embedding vector({embed_dims}),
                    mem_embedding vector({embed_dims}),
                    mem_created_at timestamptz,
                    mem_importance real,
                    mem_stability real,
                    mem_access_count integer DEFAULT 0,
                    current_ts timestamptz DEFAULT now(),
                    mem_quality_score real DEFAULT NULL
                ) RETURNS real AS $$
                DECLARE
                    cosine_sim real;
                    recency_boost real;
                    quality real;
                BEGIN
                    cosine_sim := 1.0 - (query_embedding <=> mem_embedding);
                    recency_boost := EXP(-0.00385 * EXTRACT(EPOCH FROM (current_ts - mem_created_at)) / 86400.0);
                    quality := COALESCE(mem_quality_score, 0.5);
                    RETURN (0.65 * cosine_sim)
                         + (0.15 * quality)
                         + (0.10 * COALESCE(mem_importance, 0.5))
                         + (0.10 * recency_boost);
                END;
                $$ LANGUAGE plpgsql STABLE;
            """)

        # 5. Re-embed memories
        console.print(f"[bold]4/6[/] Re-embedding {mem_count} memories...")
        async with pool.acquire() as conn:
            memories = await conn.fetch(
                "SELECT id, content, category, tags FROM memories ORDER BY created_at"
            )

        embed_client = httpx.AsyncClient(
            base_url=settings.ollama_url, timeout=300.0
        )

        async def _embed_batch(texts_batch):
            """Embed with retry on transient errors."""
            # Filter empty strings (replace with placeholder)
            safe_texts = [t if t.strip() else "empty" for t in texts_batch]
            for attempt in range(5):
                try:
                    resp = await embed_client.post(
                        "/api/embed",
                        json={"model": embed_model, "input": safe_texts, "keep_alive": "24h"},
                    )
                    if resp.status_code == 400:
                        body = resp.text[:500]
                        console.print(f"  [yellow]Batch 400: {body}[/]")
                        console.print("  [yellow]Falling back to individual...[/]")
                        results = []
                        for idx, t in enumerate(safe_texts):
                            r = await embed_client.post(
                                "/api/embed",
                                json={"model": embed_model, "input": t[:2000], "keep_alive": "24h"},
                            )
                            if r.status_code != 200:
                                console.print(f"    [red]Item {idx} failed ({r.status_code}): {r.text[:200]}[/]")
                                console.print(f"    [red]Text preview: {repr(t[:100])}[/]")
                                # Use zero vector as fallback
                                results.append([0.0] * embed_dims)
                                continue
                            results.append(r.json()["embeddings"][0])
                        return results
                    resp.raise_for_status()
                    return resp.json()["embeddings"]
                except (httpx.ReadTimeout, httpx.ConnectError) as exc:
                    wait = 2 ** attempt
                    console.print(f"  [yellow]Retry {attempt + 1}/5: {exc}[/]")
                    await asyncio.sleep(wait)
            raise RuntimeError("Embedding failed after 5 retries")

        for i in range(0, len(memories), batch_size):
            batch = memories[i : i + batch_size]
            texts = []
            for m in batch:
                # Context-enriched embedding (matches services/memory.py)
                parts = []
                if m["category"]:
                    parts.append(m["category"])
                if m["tags"]:
                    parts.append(", ".join(m["tags"]))
                embed_input = ". ".join(parts) + ". " + m["content"] if parts else m["content"]
                texts.append(embed_input[:2000])

            embeddings = await _embed_batch(texts)

            async with pool.acquire() as conn:
                for m, emb in zip(batch, embeddings):
                    await conn.execute(
                        "UPDATE memories SET embedding = $1, embedding_model = $2 WHERE id = $3",
                        np.array(emb, dtype=np.float32),
                        embed_model,
                        m["id"],
                    )

            done = min(i + batch_size, len(memories))
            console.print(f"  [green]{done}/{len(memories)}[/]")

        # 6. Re-embed entities
        console.print(f"[bold]5/6[/] Re-embedding {ent_count} entities...")
        async with pool.acquire() as conn:
            entities = await conn.fetch(
                "SELECT id, name, entity_type, description FROM entities ORDER BY created_at"
            )

        for i in range(0, len(entities), batch_size):
            batch = entities[i : i + batch_size]
            texts = [
                f"{e['entity_type']}: {e['name']} - {e['description'] or e['name']}"
                for e in batch
            ]

            embeddings = await _embed_batch(texts)

            async with pool.acquire() as conn:
                for e, emb in zip(batch, embeddings):
                    await conn.execute(
                        "UPDATE entities SET embedding = $1, embedding_model = $2 WHERE id = $3",
                        np.array(emb, dtype=np.float32),
                        embed_model,
                        e["id"],
                    )

            done = min(i + batch_size, len(entities))
            console.print(f"  [green]{done}/{len(entities)}[/]")

        await embed_client.aclose()

        # 7. Recreate halfvec HNSW indexes
        console.print("[bold]6/6[/] Rebuilding halfvec HNSW indexes...")
        async with pool.acquire() as conn:
            await conn.execute(f"""
                CREATE INDEX idx_memories_embedding_halfvec_hnsw
                ON memories USING hnsw ((embedding::halfvec({embed_dims})) halfvec_cosine_ops)
                WITH (m = 24, ef_construction = 200)
            """)
            await conn.execute(f"""
                CREATE INDEX idx_entities_embedding_halfvec_hnsw
                ON entities USING hnsw ((embedding::halfvec({embed_dims})) halfvec_cosine_ops)
                WITH (m = 24, ef_construction = 200)
            """)

        await close_pool()
        console.print(f"\n[bold green]Migration complete![/] {embed_model} ({embed_dims}d)")

    asyncio.run(_run())


@main.command("import-github")
@click.argument("owner", default="vicquick")
@click.option("--repos", help="Comma-separated repo names (default: all)")
@click.option("--machine", "-m", help="Machine name for provenance")
@click.option("--no-commits", is_flag=True, help="Skip commit history")
@click.option("--no-issues", is_flag=True, help="Skip issues/PRs")
@click.option("--no-structure", is_flag=True, help="Skip code structure/config files")
@click.option("--no-closed", is_flag=True, help="Skip closed issues/PRs")
def import_github_cmd(owner, repos, machine, no_commits, no_issues, no_structure, no_closed):
    """Import knowledge from GitHub repos (commits, issues, code structure)."""
    async def _import():
        from nobrainr.db.pool import get_pool, close_pool
        from nobrainr.db.schema import init_schema
        from nobrainr.importers.github import import_github

        pool = await get_pool()
        await init_schema(pool)
        repo_list = [r.strip() for r in repos.split(",")] if repos else None
        result = await import_github(
            owner,
            repos=repo_list,
            source_machine=machine,
            include_commits=not no_commits,
            include_issues=not no_issues,
            include_code_structure=not no_structure,
            include_closed_issues=not no_closed,
        )
        await close_pool()
        return result

    with console.status("Importing GitHub repositories..."):
        result = asyncio.run(_import())

    console.print_json(json.dumps(result, indent=2))


@main.command("normalize-categories")
def normalize_categories_cmd():
    """Normalize all memory categories to canonical set."""
    async def _normalize():
        from nobrainr.db.pool import get_pool, close_pool
        from nobrainr.db.schema import init_schema
        from nobrainr.db import queries as q
        from nobrainr.utils.categories import _CATEGORY_MAP

        pool = await get_pool()
        await init_schema(pool)
        count = await q.normalize_categories(_CATEGORY_MAP)
        await close_pool()
        return count

    console.print("[bold]Normalizing categories...[/]")
    count = asyncio.run(_normalize())
    console.print(f"[bold green]Done![/] Updated {count} memories.")


@main.command("rechunk")
@click.option("--dry-run", is_flag=True, help="Show what would be rechunked without making changes")
@click.option("--source-type", "-s", help="Only rechunk memories of this source type")
@click.option("--batch-size", "-b", default=10, help="Process N memories per batch")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def rechunk_cmd(dry_run, source_type, batch_size, yes):
    """Rechunk oversized memories that were stored before chunked ingestion.

    Finds all memories longer than chunk_threshold that don't have chunk_index
    metadata, splits them into proper overlapping chunks, stores the chunks,
    and deletes the original oversized memory.
    """
    async def _rechunk():
        from nobrainr.db.pool import get_pool, close_pool
        from nobrainr.db.schema import init_schema
        from nobrainr.db import queries as q
        from nobrainr.services.memory import store_document_chunked
        from nobrainr.config import settings

        pool = await get_pool()
        await init_schema(pool)

        # Find oversized memories without chunk metadata
        where = "WHERE length(content) > $1 AND (metadata->>'chunk_index') IS NULL"
        params = [settings.chunk_threshold]
        if source_type:
            where += " AND source_type = $2"
            params.append(source_type)

        async with pool.acquire() as conn:
            count = await conn.fetchval(
                f"SELECT count(*) FROM memories {where}", *params
            )
            if count == 0:
                console.print("[green]No oversized memories found. Everything is properly chunked![/]")
                await close_pool()
                return {"processed": 0, "chunks_created": 0, "errors": 0}

            by_type = await conn.fetch(
                f"""SELECT source_type, count(*) as cnt,
                           max(length(content)) as max_len
                    FROM memories {where}
                    GROUP BY source_type ORDER BY cnt DESC""",
                *params,
            )

        console.print(f"\n[bold]Found {count} oversized memories to rechunk:[/]")
        table = Table()
        table.add_column("Source Type", style="cyan")
        table.add_column("Count", justify="right")
        table.add_column("Max Size", justify="right")
        for row in by_type:
            table.add_row(row["source_type"], str(row["cnt"]), f"{row['max_len']:,}")
        console.print(table)

        if dry_run:
            console.print("\n[yellow]Dry run — no changes made.[/]")
            await close_pool()
            return {"processed": 0, "chunks_created": 0, "errors": 0}

        if not yes:
            click.confirm(
                f"\nThis will rechunk {count} memories (delete originals, create chunks). Continue?",
                abort=True,
            )

        processed = 0
        chunks_created = 0
        errors = 0
        offset = 0

        limit_param = f"${len(params) + 1}"

        while True:
            async with pool.acquire() as conn:
                # Always fetch from offset 0 because we delete as we go
                memories = await conn.fetch(
                    f"""SELECT id, content, summary, source_type, source_machine,
                               source_ref, tags, category, confidence,
                               metadata::text as meta_json, length(content) as content_len
                        FROM memories {where}
                        ORDER BY created_at
                        LIMIT {limit_param}""",
                    *params, batch_size,
                )

            if not memories:
                break

            for mem in memories:
                mem_id = str(mem["id"])
                content = mem["content"]
                meta = json.loads(mem["meta_json"]) if mem["meta_json"] else {}
                summary = mem["summary"]
                title = meta.get("document_title") or meta.get("file_path") or summary

                console.print(
                    f"  [{processed + 1}/{count}] [cyan]{mem['source_type']}[/] "
                    f"({mem['content_len']:,} chars) {(title or '')[:60]}"
                )

                try:
                    # Preserve original metadata but remove any stale fields
                    preserved_meta = {k: v for k, v in meta.items()
                                      if k not in ("document_id", "chunk_index",
                                                    "chunk_total", "chunk_offset")}
                    preserved_meta["rechunked_from"] = mem_id

                    result = await store_document_chunked(
                        content=content,
                        title=title,
                        summary=summary,
                        tags=list(mem["tags"]) if mem["tags"] else None,
                        category=mem["category"],
                        source_type=mem["source_type"],
                        source_machine=mem["source_machine"],
                        source_ref=mem["source_ref"],
                        confidence=float(mem["confidence"]),
                        metadata=preserved_meta,
                    )

                    n_chunks = result.get("chunks", 0)
                    chunks_created += n_chunks
                    console.print(f"    → [green]{n_chunks} chunks created[/]")

                    # Delete the original oversized memory
                    await q.delete_memory(
                        mem_id,
                        _changed_by="cli:rechunk",
                        _change_type="rechunk_replace",
                        _change_reason=f"Replaced by {n_chunks} chunks (document_id={result.get('document_id', '?')})",
                    )

                    processed += 1

                except Exception as e:
                    console.print(f"    → [red]ERROR: {e}[/]")
                    errors += 1
                    # Skip this memory on next iteration
                    offset += 1

        await close_pool()
        return {"processed": processed, "chunks_created": chunks_created, "errors": errors}

    console.print("[bold]Starting rechunk migration...[/]")
    result = asyncio.run(_rechunk())
    console.print(
        f"\n[bold green]Done![/] "
        f"Processed: {result['processed']}, "
        f"Chunks created: {result['chunks_created']}, "
        f"Errors: {result['errors']}"
    )


if __name__ == "__main__":
    main()
