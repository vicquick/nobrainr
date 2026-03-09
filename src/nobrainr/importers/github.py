"""GitHub importer — extract knowledge from all repos, commits, branches, issues, PRs.

Uses the `gh` CLI (must be authenticated) to fetch data from GitHub.

Strategy:
  1. Per repo: metadata + README → 1-2 memories
  2. Per repo: commits grouped by week → 1 memory per week of activity
  3. Per repo: issues/PRs with comments → 1 memory per significant discussion
  4. Per repo: file tree + key config files → codebase architecture
  5. Entity extraction runs on everything → projects, technologies, people, concepts
"""

import asyncio
import base64
import json
import logging
from collections import defaultdict
from datetime import datetime

from nobrainr.db.pool import get_pool
from nobrainr.services.memory import store_memory_with_extraction

logger = logging.getLogger("nobrainr.import.github")

# Cache of existing source_refs for resume support
_existing_refs: set[str] | None = None


async def _load_existing_refs() -> set[str]:
    """Load all existing GitHub source_refs so we can skip already-imported items."""
    global _existing_refs
    if _existing_refs is not None:
        return _existing_refs
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source_ref FROM memories WHERE source_type = 'github' AND source_ref IS NOT NULL"
        )
    _existing_refs = {r["source_ref"] for r in rows}
    logger.info("Loaded %d existing GitHub source_refs for resume", len(_existing_refs))
    return _existing_refs


async def _store_if_new(skip_dedup: bool = False, **kwargs) -> bool:
    """Store a memory only if source_ref doesn't already exist. Returns True if stored."""
    source_ref = kwargs.get("source_ref", "")
    if not source_ref:
        await store_memory_with_extraction(skip_dedup=skip_dedup, **kwargs)
        return True
    refs = await _load_existing_refs()
    if source_ref in refs:
        return False
    await store_memory_with_extraction(skip_dedup=skip_dedup, **kwargs)
    refs.add(source_ref)
    return True


async def _gh(args: list[str], timeout: float = 30.0) -> str:
    """Run a gh CLI command and return stdout."""
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        err = stderr.decode().strip()
        logger.warning("gh %s failed: %s", " ".join(args[:4]), err[:200])
        return ""
    return stdout.decode()


async def _gh_paginated(endpoint: str, timeout: float = 60.0) -> list:
    """Fetch a paginated REST API endpoint and return all items as a list.

    Uses --paginate and --slurp to collect all pages into a single JSON array.
    """
    raw = await _gh([
        "api", endpoint, "--paginate", "--slurp",
        "-H", "Accept: application/vnd.github+json",
    ], timeout=timeout)
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
        # --slurp wraps pages in an outer array: [[page1items], [page2items]]
        if isinstance(data, list) and data and isinstance(data[0], list):
            return [item for page in data for item in page]
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.warning("Failed to parse paginated response for %s", endpoint)
        return []


def _extract_topic_name(topic: object) -> str:
    """Safely extract a topic name from various gh API formats."""
    if isinstance(topic, str):
        return topic
    if isinstance(topic, dict):
        # GraphQL format: {"topic": {"name": "python"}}
        if "topic" in topic and isinstance(topic["topic"], dict):
            return topic["topic"].get("name", "")
        # REST format: {"name": "python"}
        if "name" in topic:
            return str(topic["name"])
    return ""


def _extract_lang_name(lang: object) -> str:
    """Safely extract a language name from various gh API formats."""
    if isinstance(lang, str):
        return lang
    if isinstance(lang, dict):
        # GraphQL: {"node": {"name": "Python"}}
        if "node" in lang and isinstance(lang["node"], dict):
            return lang["node"].get("name", "")
        if "name" in lang:
            return str(lang["name"])
    return ""


async def _import_repo_overview(
    owner: str, repo: str, repo_data: dict,
    source_machine: str | None, sem: asyncio.Semaphore,
) -> int:
    """Store repo metadata + README as memories."""
    stored = 0
    name = repo_data.get("name", repo)
    desc = repo_data.get("description", "") or ""
    langs = repo_data.get("languages", [])
    topics = repo_data.get("repositoryTopics", [])
    is_private = repo_data.get("isPrivate", False)
    default_branch = repo_data.get("defaultBranchRef", {})
    if isinstance(default_branch, dict):
        default_branch = default_branch.get("name", "main")
    else:
        default_branch = str(default_branch) if default_branch else "main"

    # Parse languages and topics robustly
    lang_names = [n for lang in (langs or []) if (n := _extract_lang_name(lang))]
    lang_str = ", ".join(lang_names) if lang_names else "unknown"

    topic_names = [n for t in (topics or []) if (n := _extract_topic_name(t))]
    topic_str = ", ".join(topic_names) if topic_names else "none"

    visibility = "private" if is_private else "public"

    # Get branches via REST
    branches_data = await _gh_paginated(f"repos/{owner}/{repo}/branches")
    branches = [b.get("name", "") for b in branches_data if isinstance(b, dict) and b.get("name")]
    if not branches:
        branches = [default_branch]

    branch_str = ", ".join(branches[:20])
    if len(branches) > 20:
        branch_str += f" (+ {len(branches) - 20} more)"

    overview = (
        f"## GitHub Repository: {owner}/{name}\n\n"
        f"**Description:** {desc or 'No description'}\n"
        f"**Visibility:** {visibility}\n"
        f"**Default branch:** {default_branch}\n"
        f"**Languages:** {lang_str}\n"
        f"**Topics:** {topic_str}\n"
        f"**Branches ({len(branches)}):** {branch_str}\n"
    )

    # Ensure all tags are strings
    repo_tags = ["github", "repository", name] + [t for t in topic_names if t]

    async with sem:
        try:
            if await _store_if_new(
                content=overview,
                summary=f"GitHub repo: {owner}/{name} — {desc[:100] if desc else visibility}",
                category="architecture",
                tags=repo_tags,
                source_type="github",
                source_machine=source_machine,
                source_ref=f"github:{owner}/{name}",
                confidence=0.9,
            ):
                stored += 1
                logger.info("  Stored overview for %s/%s", owner, name)
        except Exception:
            logger.exception("Failed to store repo overview for %s/%s", owner, name)

    # README
    readme_raw = await _gh([
        "api", f"repos/{owner}/{repo}/readme",
        "-H", "Accept: application/vnd.github+json",
    ], timeout=15)
    if readme_raw.strip():
        try:
            readme_json = json.loads(readme_raw)
            readme_b64 = readme_json.get("content", "")
            readme_text = base64.b64decode(readme_b64).decode("utf-8", errors="replace")
            if len(readme_text) > 5000:
                readme_text = readme_text[:5000] + "\n\n[... truncated]"

            if len(readme_text) > 50:
                async with sem:
                    try:
                        if await _store_if_new(
                            content=f"## README: {owner}/{name}\n\n{readme_text}",
                            summary=f"README for {owner}/{name}",
                            category="documentation",
                            tags=["github", "readme", name],
                            source_type="github",
                            source_machine=source_machine,
                            source_ref=f"github:{owner}/{name}/README",
                            confidence=0.85,
                        ):
                            stored += 1
                            logger.info("  Stored README for %s/%s", owner, name)
                    except Exception:
                        logger.exception("Failed to store README for %s/%s", owner, name)
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Failed to decode README for %s/%s: %s", owner, name, exc)

    return stored


async def _import_commits(
    owner: str, repo: str, default_branch: str,
    source_machine: str | None, sem: asyncio.Semaphore,
) -> int:
    """Fetch ALL commits from ALL branches. Each commit → 1 memory."""
    stored = 0

    # Fetch all branches
    branches_data = await _gh_paginated(f"repos/{owner}/{repo}/branches?per_page=100")
    branch_names = [b.get("name", "") for b in branches_data if isinstance(b, dict) and b.get("name")]
    if not branch_names:
        branch_names = [default_branch]

    logger.info("  Fetching commits from %d branches for %s/%s", len(branch_names), owner, repo)

    # Collect commits from all branches, dedup by full SHA
    # Track which branches each commit appears on
    seen_shas: dict[str, list[str]] = {}
    commits = []
    for branch in branch_names:
        branch_commits = await _gh_paginated(f"repos/{owner}/{repo}/commits?sha={branch}&per_page=100")
        new_in_branch = 0
        for c in branch_commits:
            if not isinstance(c, dict):
                continue
            full_sha = c.get("sha", "")
            if not full_sha:
                continue
            if full_sha in seen_shas:
                seen_shas[full_sha].append(branch)
                continue
            seen_shas[full_sha] = [branch]
            commit_obj = c.get("commit", {})
            if not isinstance(commit_obj, dict):
                continue
            author_obj = commit_obj.get("author", {}) or {}
            committer_obj = commit_obj.get("committer", {}) or {}
            commits.append({
                "sha": full_sha,
                "sha_short": full_sha[:8],
                "message": commit_obj.get("message", ""),
                "date": author_obj.get("date", ""),
                "author": author_obj.get("name", c.get("author", {}).get("login", "") if isinstance(c.get("author"), dict) else ""),
                "committer": committer_obj.get("name", ""),
                "first_branch": branch,
            })
            new_in_branch += 1
        if new_in_branch > 0:
            logger.info("    Branch %s: %d new commits", branch, new_in_branch)

    if not commits:
        logger.info("  No commits found for %s/%s", owner, repo)
        return 0

    logger.info("  Found %d unique commits across %d branches for %s/%s", len(commits), len(branch_names), owner, repo)

    # Store EACH commit as its own memory
    for c in commits:
        sha = c["sha"]
        sha_short = c["sha_short"]
        message = c["message"]
        date = c["date"][:10] if c["date"] else "unknown"
        author = c["author"]
        branches = seen_shas.get(sha, [c["first_branch"]])
        branch_str = ", ".join(branches[:5])
        if len(branches) > 5:
            branch_str += f" (+{len(branches) - 5} more)"

        # Full commit message — title + body
        msg_lines = message.split("\n")
        title = msg_lines[0][:300]
        body = "\n".join(msg_lines[1:]).strip()

        content = (
            f"## Commit: {owner}/{repo} `{sha_short}`\n\n"
            f"**Date:** {date}\n"
            f"**Author:** {author}\n"
            f"**Branch(es):** {branch_str}\n\n"
            f"### {title}\n"
        )
        if body:
            content += f"\n{body[:3000]}\n"

        if len(content) < 30:
            continue

        async with sem:
            try:
                if await _store_if_new(
                    skip_dedup=True,  # Each commit has unique SHA, no dedup needed
                    content=content,
                    summary=f"Commit {sha_short} ({date}): {title[:120]}",
                    category="architecture",
                    tags=["github", "commit", repo, c["first_branch"]],
                    source_type="github",
                    source_machine=source_machine,
                    source_ref=f"github:{owner}/{repo}/commit/{sha_short}",
                    confidence=0.75,
                ):
                    stored += 1
                    if stored % 25 == 0:
                        logger.info("  ... stored %d commits so far for %s/%s", stored, owner, repo)
            except Exception:
                logger.exception("Failed to store commit %s for %s/%s", sha_short, owner, repo)

    logger.info("  Stored %d individual commit memories for %s/%s", stored, owner, repo)
    return stored


async def _import_issues_prs(
    owner: str, repo: str,
    source_machine: str | None, sem: asyncio.Semaphore,
    include_closed: bool = True,
) -> int:
    """Import issues and PRs with their comments."""
    stored = 0

    # Fetch issues via REST with state param in URL (GET, not POST)
    state = "all" if include_closed else "open"
    issues_data = await _gh_paginated(
        f"repos/{owner}/{repo}/issues?state={state}&per_page=100"
    )

    if not issues_data:
        logger.info("  No issues/PRs found for %s/%s", owner, repo)
        return 0

    logger.info("  Found %d issues/PRs for %s/%s", len(issues_data), owner, repo)

    for issue in issues_data:
        if not isinstance(issue, dict):
            continue

        number = issue.get("number", 0)
        title = issue.get("title", "")
        body = issue.get("body", "") or ""
        issue_state = issue.get("state", "")
        is_pr = "pull_request" in issue
        labels = [lbl.get("name", "") for lbl in issue.get("labels", []) if isinstance(lbl, dict)]
        user_obj = issue.get("user", {})
        author = user_obj.get("login", "") if isinstance(user_obj, dict) else ""
        created = issue.get("created_at", "")[:10]
        kind = "PR" if is_pr else "Issue"

        # Skip trivial issues (no body, no comments)
        comments_count = issue.get("comments", 0)
        if not body and comments_count == 0:
            continue

        # Fetch comments for substantial discussions
        comments_text = ""
        if comments_count > 0:
            comments_url = f"repos/{owner}/{repo}/issues/{number}/comments?per_page=10"
            comments_raw = await _gh([
                "api", comments_url,
                "-H", "Accept: application/vnd.github+json",
            ], timeout=15)
            if comments_raw.strip():
                try:
                    comments_data = json.loads(comments_raw)
                    if isinstance(comments_data, list):
                        comment_lines = []
                        for cm in comments_data[:10]:
                            if isinstance(cm, dict):
                                cm_user = cm.get("user", {})
                                cm_author = cm_user.get("login", "") if isinstance(cm_user, dict) else ""
                                cm_body = (cm.get("body", "") or "")[:500]
                                if cm_body:
                                    comment_lines.append(f"**{cm_author}:** {cm_body}")
                        if comment_lines:
                            comments_text = "\n\n### Comments\n" + "\n\n".join(comment_lines)
                except json.JSONDecodeError:
                    pass

        # Build memory content
        content = (
            f"## {kind} #{number}: {title}\n\n"
            f"**Repo:** {owner}/{repo}\n"
            f"**Author:** {author} | **State:** {issue_state} | **Created:** {created}\n"
        )
        if labels:
            content += f"**Labels:** {', '.join(str(lbl) for lbl in labels)}\n"
        content += f"\n{body[:3000]}"
        if comments_text:
            content += f"\n{comments_text}"

        # Truncate if too long
        if len(content) > 5500:
            content = content[:5500] + "\n\n[... truncated]"

        if len(content) < 50:
            continue

        async with sem:
            try:
                if await _store_if_new(
                    content=content,
                    summary=f"{kind} #{number} ({issue_state}): {title[:100]}",
                    category="architecture" if is_pr else "debugging",
                    tags=["github", kind.lower(), repo] + [str(lbl) for lbl in labels[:5]],
                    source_type="github",
                    source_machine=source_machine,
                    source_ref=f"github:{owner}/{repo}/{kind.lower()}/{number}",
                    confidence=0.8,
                ):
                    stored += 1
            except Exception:
                logger.exception("Failed to store %s #%d for %s/%s", kind, number, owner, repo)

    logger.info("  Stored %d issue/PR memories for %s/%s", stored, owner, repo)
    return stored


async def _import_code_structure(
    owner: str, repo: str, default_branch: str,
    source_machine: str | None, sem: asyncio.Semaphore,
) -> int:
    """Import file tree and key config files to capture codebase architecture."""
    stored = 0

    # Get file tree (recursive) — pass recursive as query param, not -f
    tree_raw = await _gh([
        "api", f"repos/{owner}/{repo}/git/trees/{default_branch}?recursive=1",
        "-H", "Accept: application/vnd.github+json",
    ], timeout=30)

    if not tree_raw.strip():
        logger.info("  No tree data for %s/%s", owner, repo)
        return 0

    try:
        tree_data = json.loads(tree_raw)
    except json.JSONDecodeError:
        logger.warning("Failed to parse tree for %s/%s", owner, repo)
        return 0

    tree_items = tree_data.get("tree", [])
    if not tree_items:
        return 0

    # Build file tree summary
    dirs = set()
    files_by_ext: dict[str, int] = defaultdict(int)
    key_files: list[str] = []

    KEY_FILENAMES = {
        "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        ".env.example", "Makefile", "Procfile",
        "requirements.txt", "setup.py", "setup.cfg",
        "tsconfig.json", "vite.config.ts", "vite.config.js",
        "nuxt.config.ts", "next.config.js", "next.config.ts",
        "CLAUDE.md",
    }

    for item in tree_items:
        path = item.get("path", "")
        item_type = item.get("type", "")

        if item_type == "tree":
            dirs.add(path)
        elif item_type == "blob":
            ext = path.rsplit(".", 1)[-1] if "." in path else "none"
            files_by_ext[ext] += 1

            basename = path.rsplit("/", 1)[-1] if "/" in path else path
            if basename in KEY_FILENAMES or path in KEY_FILENAMES:
                key_files.append(path)

    # Store file tree overview
    top_dirs = sorted(d for d in dirs if "/" not in d)
    ext_summary = ", ".join(f".{k}: {v}" for k, v in sorted(files_by_ext.items(), key=lambda x: -x[1])[:15])
    tree_content = (
        f"## Codebase Structure: {owner}/{repo}\n\n"
        f"**Total files:** {sum(files_by_ext.values())}\n"
        f"**File types:** {ext_summary}\n"
        f"**Top-level dirs:** {', '.join(top_dirs[:20])}\n"
    )

    # Add directory tree (top 2 levels)
    tree_lines = []
    for d in sorted(dirs):
        depth = d.count("/")
        if depth <= 1:
            indent = "  " * depth
            tree_lines.append(f"{indent}{d}/")
    if tree_lines:
        tree_content += "\n**Directory tree:**\n```\n" + "\n".join(tree_lines[:60]) + "\n```\n"

    async with sem:
        try:
            if await _store_if_new(
                content=tree_content,
                summary=f"Codebase structure: {owner}/{repo} ({sum(files_by_ext.values())} files)",
                category="architecture",
                tags=["github", "codebase", "structure", repo],
                source_type="github",
                source_machine=source_machine,
                source_ref=f"github:{owner}/{repo}/tree",
                confidence=0.85,
            ):
                stored += 1
        except Exception:
            logger.exception("Failed to store tree for %s/%s", owner, repo)

    # Fetch and store key config files
    for filepath in key_files[:10]:
        file_raw = await _gh([
            "api", f"repos/{owner}/{repo}/contents/{filepath}",
            "-H", "Accept: application/vnd.github+json",
        ], timeout=10)
        if not file_raw.strip():
            continue

        try:
            file_json = json.loads(file_raw)
            file_b64 = file_json.get("content", "")
            decoded = base64.b64decode(file_b64).decode("utf-8", errors="replace")
        except Exception:
            continue

        if len(decoded) < 20 or len(decoded) > 5000:
            continue

        async with sem:
            try:
                if await _store_if_new(
                    content=f"## Config File: {owner}/{repo}/{filepath}\n\n```\n{decoded}\n```",
                    summary=f"Config: {owner}/{repo}/{filepath}",
                    category="architecture",
                    tags=["github", "config", repo, filepath.rsplit("/", 1)[-1]],
                    source_type="github",
                    source_machine=source_machine,
                    source_ref=f"github:{owner}/{repo}/file/{filepath}",
                    confidence=0.85,
                ):
                    stored += 1
            except Exception:
                logger.exception("Failed to store config %s for %s/%s", filepath, owner, repo)

    logger.info("  Stored %d structure memories for %s/%s", stored, owner, repo)
    return stored


async def import_github(
    owner: str = "vicquick",
    *,
    repos: list[str] | None = None,
    source_machine: str | None = None,
    include_commits: bool = True,
    include_issues: bool = True,
    include_code_structure: bool = True,
    include_closed_issues: bool = True,
    concurrency: int = 2,
) -> dict:
    """Import knowledge from GitHub repositories.

    Args:
        owner: GitHub username or org.
        repos: Specific repos to import (default: all).
        source_machine: Machine identifier for provenance.
        include_commits: Import commit history.
        include_issues: Import issues and PRs.
        include_code_structure: Import file tree and key config files.
        include_closed_issues: Include closed issues/PRs.
        concurrency: Max concurrent store operations.

    Returns:
        Summary dict with counts.
    """
    sem = asyncio.Semaphore(concurrency)

    # List repos
    if repos:
        repo_list = [{"name": r} for r in repos]
    else:
        raw_text = await _gh([
            "repo", "list", owner, "--limit", "100",
            "--json", "name,description,isPrivate,pushedAt,defaultBranchRef,languages,repositoryTopics",
        ], timeout=30)
        if raw_text.strip():
            try:
                repo_list = json.loads(raw_text)
                if not isinstance(repo_list, list):
                    repo_list = []
            except json.JSONDecodeError:
                repo_list = []
        else:
            repo_list = []

    if not repo_list:
        return {"error": "No repos found", "owner": owner}

    total_repos = len(repo_list)
    total_overview = 0
    total_commits = 0
    total_issues = 0
    total_structure = 0
    errors = 0

    logger.info("Importing %d repos from %s", total_repos, owner)

    for i, repo_data in enumerate(repo_list):
        name = repo_data.get("name", "")
        if not name:
            continue

        default_branch = repo_data.get("defaultBranchRef", {})
        if isinstance(default_branch, dict):
            default_branch = default_branch.get("name", "main")
        elif default_branch:
            default_branch = str(default_branch)
        else:
            default_branch = "main"

        logger.info("[%d/%d] Importing %s/%s...", i + 1, total_repos, owner, name)

        try:
            # 1. Repo overview + README
            count = await _import_repo_overview(owner, name, repo_data, source_machine, sem)
            total_overview += count

            # 2. Commits
            if include_commits:
                count = await _import_commits(owner, name, default_branch, source_machine, sem)
                total_commits += count

            # 3. Code structure + config files
            if include_code_structure:
                count = await _import_code_structure(owner, name, default_branch, source_machine, sem)
                total_structure += count

            # 4. Issues/PRs
            if include_issues:
                count = await _import_issues_prs(owner, name, source_machine, sem, include_closed_issues)
                total_issues += count

        except Exception:
            logger.exception("Failed to import %s/%s", owner, name)
            errors += 1

    total_all = total_overview + total_commits + total_structure + total_issues
    result = {
        "repos": total_repos,
        "overview_memories": total_overview,
        "commit_memories": total_commits,
        "structure_memories": total_structure,
        "issue_pr_memories": total_issues,
        "total_memories": total_all,
        "errors": errors,
        "owner": owner,
    }

    logger.info(
        "GitHub import complete: %d repos → %d overview, %d commits, %d structure, %d issues/PRs (%d errors)",
        total_repos, total_overview, total_commits, total_structure, total_issues, errors,
    )

    return result
