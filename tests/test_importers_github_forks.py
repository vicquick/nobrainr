"""Fork-filter behavior for import_github.

The pre-fix importer asked gh for a JSON field list that did not include
``isFork``, and never filtered. One forked repo (QGIS, ~70k upstream commits)
was flooding every github_sync run. This test locks the filter in place.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from nobrainr.importers import github as importer


_FAKE_REPOS = [
    {"name": "nobrainr", "isFork": False, "defaultBranchRef": {"name": "main"}},
    {"name": "QGIS", "isFork": True, "defaultBranchRef": {"name": "master"}},
    {"name": "flux-mcp", "isFork": False, "defaultBranchRef": {"name": "main"}},
]


@pytest.fixture(autouse=True)
def _reset_refs_cache():
    """_existing_refs is module-level state; reset between tests."""
    importer._existing_refs = set()
    yield
    importer._existing_refs = None


async def _noop(*args, **kwargs):
    return 0


@pytest.mark.asyncio
async def test_bulk_import_skips_forks_by_default():
    """include_forks=False (default) drops QGIS before any per-repo work runs."""
    gh_mock = AsyncMock(return_value=json.dumps(_FAKE_REPOS))
    overview_mock = AsyncMock(return_value=0)

    with (
        patch.object(importer, "_gh", gh_mock),
        patch.object(importer, "_import_repo_overview", overview_mock),
        patch.object(importer, "_import_commits", _noop),
        patch.object(importer, "_import_code_structure", _noop),
        patch.object(importer, "_import_issues_prs", _noop),
    ):
        result = await importer.import_github(
            "vicquick",
            include_commits=False,
            include_issues=False,
            include_code_structure=False,
            include_source_code=False,
        )

    assert result["skipped_forks"] == 1
    assert result["repos"] == 2  # nobrainr + flux-mcp, QGIS dropped
    assert result["include_forks"] is False

    # _import_repo_overview must NOT have been called with QGIS
    called_repos = {call.args[1] for call in overview_mock.await_args_list}
    assert "QGIS" not in called_repos
    assert called_repos == {"nobrainr", "flux-mcp"}


@pytest.mark.asyncio
async def test_bulk_import_include_forks_true_keeps_everything():
    """Explicit opt-in imports forks alongside owned repos."""
    gh_mock = AsyncMock(return_value=json.dumps(_FAKE_REPOS))
    overview_mock = AsyncMock(return_value=0)

    with (
        patch.object(importer, "_gh", gh_mock),
        patch.object(importer, "_import_repo_overview", overview_mock),
        patch.object(importer, "_import_commits", _noop),
        patch.object(importer, "_import_code_structure", _noop),
        patch.object(importer, "_import_issues_prs", _noop),
    ):
        result = await importer.import_github(
            "vicquick",
            include_forks=True,
            include_commits=False,
            include_issues=False,
            include_code_structure=False,
            include_source_code=False,
        )

    assert result["skipped_forks"] == 0
    assert result["repos"] == 3
    called_repos = {call.args[1] for call in overview_mock.await_args_list}
    assert called_repos == {"nobrainr", "QGIS", "flux-mcp"}


@pytest.mark.asyncio
async def test_explicit_repos_list_bypasses_fork_filter():
    """repos=[...] means the caller already picked — trust them."""
    overview_mock = AsyncMock(return_value=0)

    with (
        patch.object(importer, "_import_repo_overview", overview_mock),
        patch.object(importer, "_import_commits", _noop),
        patch.object(importer, "_import_code_structure", _noop),
        patch.object(importer, "_import_issues_prs", _noop),
    ):
        result = await importer.import_github(
            "vicquick",
            repos=["QGIS"],  # explicit fork request
            include_commits=False,
            include_issues=False,
            include_code_structure=False,
            include_source_code=False,
        )

    assert result["skipped_forks"] == 0
    assert result["repos"] == 1
    called_repos = {call.args[1] for call in overview_mock.await_args_list}
    assert called_repos == {"QGIS"}


@pytest.mark.asyncio
async def test_gh_repo_list_requests_isfork_field():
    """Regression guard: the gh CLI query must ask for isFork.

    Previous bug: the --json field list did not include isFork, so every repo
    came back with isFork=None and the filter had nothing to check.
    """
    gh_mock = AsyncMock(return_value=json.dumps([]))

    with (
        patch.object(importer, "_gh", gh_mock),
        patch.object(importer, "_import_repo_overview", _noop),
        patch.object(importer, "_import_commits", _noop),
        patch.object(importer, "_import_code_structure", _noop),
        patch.object(importer, "_import_issues_prs", _noop),
    ):
        await importer.import_github("vicquick")

    # Find the 'repo list' call
    repo_list_call = None
    for call in gh_mock.await_args_list:
        args = call.args[0] if call.args else call.kwargs.get("args", [])
        if len(args) >= 2 and args[0] == "repo" and args[1] == "list":
            repo_list_call = args
            break

    assert repo_list_call is not None, "import_github did not call `gh repo list`"
    json_fields_idx = repo_list_call.index("--json") + 1
    fields = repo_list_call[json_fields_idx].split(",")
    assert "isFork" in fields, f"isFork missing from gh repo list --json: {fields}"
