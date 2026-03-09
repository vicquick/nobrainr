"""Code indexing service — AST-based symbol extraction for token-efficient retrieval.

Inspired by jcodemunch-mcp. Extracts code symbols (functions, classes, methods)
using Python's ast module and stores them as memories with structured metadata.
Enables agents to search code at the symbol level instead of reading entire files.
"""

import ast
import logging
from pathlib import Path

from nobrainr.services.memory import store_memory_with_extraction

logger = logging.getLogger("nobrainr")

# File extensions we can parse
PARSEABLE_EXTENSIONS = {".py"}

# Max files to index in one call
MAX_FILES = 500


def _extract_python_symbols(file_path: str, source: str) -> list[dict]:
    """Extract symbols from a Python source file using ast."""
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        logger.debug("Syntax error in %s, skipping", file_path)
        return []

    symbols = []

    # Only process top-level definitions (not nested via ast.walk)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            sig = _build_function_signature(node)
            docstring = ast.get_docstring(node) or ""
            symbols.append({
                "kind": "function",
                "name": node.name,
                "qualified_name": node.name,
                "signature": sig,
                "docstring": docstring[:500],
                "line_number": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "file_path": file_path,
                "is_async": isinstance(node, ast.AsyncFunctionDef),
            })

        elif isinstance(node, ast.ClassDef):
            docstring = ast.get_docstring(node) or ""
            bases = [_name_of(b) for b in node.bases]
            symbols.append({
                "kind": "class",
                "name": node.name,
                "qualified_name": node.name,
                "signature": f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}",
                "docstring": docstring[:500],
                "line_number": node.lineno,
                "end_line": getattr(node, "end_lineno", node.lineno),
                "file_path": file_path,
            })

            # Extract methods
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    method_sig = _build_function_signature(item)
                    method_doc = ast.get_docstring(item) or ""
                    symbols.append({
                        "kind": "method",
                        "name": item.name,
                        "qualified_name": f"{node.name}.{item.name}",
                        "signature": method_sig,
                        "docstring": method_doc[:500],
                        "line_number": item.lineno,
                        "end_line": getattr(item, "end_lineno", item.lineno),
                        "file_path": file_path,
                        "class_name": node.name,
                        "is_async": isinstance(item, ast.AsyncFunctionDef),
                    })

    return symbols


def _build_function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Build a function signature string from an AST node."""
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    args_parts = []

    # Regular args
    args = node.args
    defaults_offset = len(args.args) - len(args.defaults)

    for i, arg in enumerate(args.args):
        name = arg.arg
        annotation = _annotation_str(arg.annotation)
        if annotation:
            name = f"{name}: {annotation}"
        default_idx = i - defaults_offset
        if default_idx >= 0:
            name = f"{name}=..."
        args_parts.append(name)

    # *args
    if args.vararg:
        name = f"*{args.vararg.arg}"
        annotation = _annotation_str(args.vararg.annotation)
        if annotation:
            name = f"*{args.vararg.arg}: {annotation}"
        args_parts.append(name)

    # **kwargs
    if args.kwarg:
        name = f"**{args.kwarg.arg}"
        annotation = _annotation_str(args.kwarg.annotation)
        if annotation:
            name = f"**{args.kwarg.arg}: {annotation}"
        args_parts.append(name)

    returns = _annotation_str(node.returns)
    ret_str = f" -> {returns}" if returns else ""

    return f"{prefix}def {node.name}({', '.join(args_parts)}){ret_str}"


def _annotation_str(node) -> str:
    """Convert an annotation AST node to a string."""
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def _name_of(node) -> str:
    """Get the name string from a Name or Attribute node."""
    try:
        return ast.unparse(node)
    except Exception:
        return "?"


async def index_directory(
    directory: str,
    *,
    tags: list[str] | None = None,
    source_machine: str | None = None,
    extensions: list[str] | None = None,
) -> dict:
    """Index a directory by extracting code symbols and storing as memories.

    Returns:
        {"status": "indexed", "files": N, "symbols": N, "stored": N, "skipped": N}
    """
    root = Path(directory)
    exts = set(extensions or [".py"])
    all_tags = list(tags or []) + ["code", "indexed"]

    # Find files
    files = []
    for ext in exts:
        files.extend(root.rglob(f"*{ext}"))

    # Filter out common non-code directories
    skip_dirs = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".tox", "dist", "build"}
    files = [f for f in files if not any(p in skip_dirs for p in f.parts)]
    files = files[:MAX_FILES]

    total_symbols = 0
    stored = 0
    skipped = 0

    for file_path in files:
        try:
            source = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        if file_path.suffix == ".py":
            symbols = _extract_python_symbols(str(file_path), source)
        else:
            continue  # Only Python for now

        total_symbols += len(symbols)

        for sym in symbols:
            # Build compact content: signature + docstring
            content_parts = [sym["signature"]]
            if sym["docstring"]:
                content_parts.append(sym["docstring"])
            content = "\n".join(content_parts)

            # Build metadata
            metadata = {
                "kind": sym["kind"],
                "qualified_name": sym["qualified_name"],
                "signature": sym["signature"],
                "docstring": sym["docstring"][:200],
                "file_path": str(file_path.relative_to(root)),
                "line_number": sym["line_number"],
                "end_line": sym["end_line"],
                "index_root": directory,
            }
            if sym.get("class_name"):
                metadata["class_name"] = sym["class_name"]
            if sym.get("is_async"):
                metadata["is_async"] = True

            sym_tags = all_tags + [f"kind:{sym['kind']}"]
            if sym.get("class_name"):
                sym_tags.append(f"class:{sym['class_name']}")

            try:
                result = await store_memory_with_extraction(
                    content=content,
                    summary=f"{sym['kind']}: {sym['qualified_name']} in {metadata['file_path']}:{sym['line_number']}",
                    tags=sym_tags,
                    category="code",
                    source_type="code_index",
                    source_machine=source_machine,
                    source_ref=f"{file_path}:{sym['line_number']}",
                    confidence=0.9,
                    metadata=metadata,
                    skip_dedup=True,
                )
                if result.get("status") in ("stored", "updated", "superseded"):
                    stored += 1
                else:
                    skipped += 1
            except Exception:
                logger.debug("Failed to store symbol %s", sym["qualified_name"])
                skipped += 1

    return {
        "status": "indexed",
        "directory": directory,
        "files_scanned": len(files),
        "symbols_found": total_symbols,
        "stored": stored,
        "skipped": skipped,
    }
