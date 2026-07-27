"""Library layer (2026-07-27): scope filtering, citation bounds, tool contract."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from nobrainr.config import settings
from nobrainr.dashboard.api import _library_scope_filter
from nobrainr.mcp import server as mcp_server


def _unwrap(fn):
    return fn.fn if hasattr(fn, "fn") else fn


library_search = _unwrap(mcp_server.library_search)

HITS = [
    {"id": "1", "source_type": "docx", "metadata": {"file_path": "thesis.docx"},
     "content": "geodesy content", "summary": "s1"},
    {"id": "2", "source_type": "docx", "metadata": '{"file_path": "other.docx"}',
     "content": "other content", "summary": "s2"},
    {"id": "3", "source_type": "github", "metadata": {}, "content": "commit",
     "summary": "s3"},
    {"id": "4", "source_type": "affine_memos", "metadata": None,
     "source_ref": "memo-42", "content": "memo", "summary": "s4"},
]


# ── _library_scope_filter (pure)


def test_scope_excludes_non_library_types():
    out = _library_scope_filter(HITS, ref="")
    assert [h["id"] for h in out] == ["1", "2", "4"]  # github dropped


def test_scope_to_one_document_dict_metadata():
    out = _library_scope_filter(HITS, ref="thesis.docx")
    assert [h["id"] for h in out] == ["1"]


def test_scope_to_one_document_string_metadata():
    # metadata as JSON STRING (queued-write path shape) must still match
    out = _library_scope_filter(HITS, ref="other.docx")
    assert [h["id"] for h in out] == ["2"]


def test_scope_source_ref_fallback():
    out = _library_scope_filter(HITS, ref="memo-42")
    assert [h["id"] for h in out] == ["4"]


# ── library_search tool (mocked search + rerank)


async def test_tool_scopes_and_shapes_output():
    with (
        patch.object(mcp_server, "embed_text", AsyncMock(return_value=[0.1])),
        patch.object(mcp_server.queries, "search_memories",
                     AsyncMock(return_value=list(HITS))),
        patch("nobrainr.services.reranker.rerank",
              AsyncMock(side_effect=lambda q, h, limit: h[:limit])),
    ):
        out = await library_search(query="geodesy", document="thesis.docx")
    assert [h["id"] for h in out["hits"]] == ["1"]
    assert out["documents"] == ["thesis.docx"]
    assert "answer" not in out  # synthesize defaults off


async def test_tool_synthesis_citations_bounded():
    resp = {"answer": "It says X.", "citations": [0, 7, -1]}  # 7/-1 hallucinated
    with (
        patch.object(mcp_server, "embed_text", AsyncMock(return_value=[0.1])),
        patch.object(mcp_server.queries, "search_memories",
                     AsyncMock(return_value=[HITS[0]])),
        patch("nobrainr.services.reranker.rerank",
              AsyncMock(side_effect=lambda q, h, limit: h[:limit])),
        patch("nobrainr.extraction.llm.ollama_chat", AsyncMock(return_value=resp)),
    ):
        out = await library_search(query="q", synthesize=True)
    assert out["answer"] == "It says X."
    assert out["citations"] == [0]  # out-of-range citations dropped


async def test_tool_rerank_failure_degrades():
    with (
        patch.object(mcp_server, "embed_text", AsyncMock(return_value=[0.1])),
        patch.object(mcp_server.queries, "search_memories",
                     AsyncMock(return_value=list(HITS))),
        patch("nobrainr.services.reranker.rerank",
              AsyncMock(side_effect=RuntimeError("reranker down"))),
    ):
        out = await library_search(query="q")
    assert len(out["hits"]) == 3  # scoped hits survive without rerank


def test_library_source_types_config():
    assert "docx" in settings.library_source_types
    assert "affine_memos" in settings.library_source_types
