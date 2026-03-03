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
    """Start the MCP server (SSE transport)."""
    from nobrainr.mcp.server import mcp
    console.print("[bold green]Starting nobrainr MCP server...[/]")
    mcp.run(transport="sse")


@main.command()
def status():
    """Check database and embedding model status."""
    async def _status():
        from nobrainr.db.pool import get_pool, close_pool
        from nobrainr.db.schema import init_schema
        from nobrainr.embeddings.ollama import check_model
        from nobrainr.db import queries

        try:
            pool = await get_pool()
            await init_schema(pool)
            stats = await queries.get_stats()
            model_ok = await check_model()
            await close_pool()
            return stats, model_ok
        except Exception as e:
            return None, str(e)

    stats, model_ok = asyncio.run(_status())

    if stats is None:
        console.print(f"[bold red]Database connection failed:[/] {model_ok}")
        sys.exit(1)

    table = Table(title="nobrainr Status")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Total memories", str(stats["total_memories"]))
    table.add_row("Raw conversations", str(stats["raw_conversations"]))
    table.add_row("Embedding model", "OK" if model_ok is True else "NOT FOUND")

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
        console.print(f"\n[bold cyan]#{i}[/] [dim](similarity: {sim:.4f})[/]")
        if mem.get("summary"):
            console.print(f"  [bold]{mem['summary']}[/]")
        console.print(f"  {mem['content'][:200]}...")
        tags_str = ", ".join(mem.get("tags", []))
        if tags_str:
            console.print(f"  [dim]Tags: {tags_str}[/]")


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
@click.option("--machine", "-m", help="Machine name (e.g. 'myserver', 'workserver')")
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
