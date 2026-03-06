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


if __name__ == "__main__":
    main()
