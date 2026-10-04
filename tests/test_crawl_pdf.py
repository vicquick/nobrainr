"""PDF crawling without Crawl4AI's browser (crawler/pdf.py + client routing).

Crawl4AI 0.9 fetched and scraped every PDF, then failed it with "Blocked by
anti-bot protection: Cloudflare JS challenge" and a bare HTTP 500. These tests
lock in the direct path that replaced it:
- text-layer extraction into page-separated markdown (title, page counts,
  truncation, scanned and encrypted PDFs as clear errors)
- the fetch decides "is a PDF" by the %PDF- magic, caps size, and refuses
  internal hosts on every redirect hop
- routing: .pdf URLs go direct; other URLs only after a failed Crawl4AI crawl
- Crawl4AI HTTP errors carry status, correlation_id and a hint
- the dead /crawl/job API falls back to the synchronous endpoint
No network: generated PDFs, httpx.MockTransport, patched DNS.
"""

from __future__ import annotations

import functools
from unittest.mock import AsyncMock, patch

import httpx
import pymupdf
import pytest

from nobrainr.config import settings
from nobrainr.crawler import client as crawl_client
from nobrainr.crawler import pdf as crawl_pdf_mod
from nobrainr.mcp import server as mcp_server


def _unwrap(fn):
    if hasattr(fn, "fn"):
        return fn.fn
    if hasattr(fn, "__wrapped__"):
        return fn.__wrapped__
    return fn


crawl_page = _unwrap(mcp_server.crawl_page)
crawl_and_store = _unwrap(mcp_server.crawl_and_store)


def _make_pdf(pages: list[str], *, title: str = "", password: str = "") -> bytes:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text, fontsize=11)
    if title:
        doc.set_metadata({"title": title, "author": "Test Author"})
    if password:
        data = doc.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw=password, user_pw=password,
        )
    else:
        data = doc.tobytes()
    doc.close()
    return data


PDF = _make_pdf(
    ["Erste Seite mit Text.", "", "Dritte Seite, Bewehrung nach DIN 1045."],
    title="Testplan",
)


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Every hostname resolves to a public address unless a test says otherwise."""
    async def resolve(host: str) -> list[str]:
        return {"internal.test": ["10.0.0.5"], "rebind.test": ["93.184.216.34", "127.0.0.1"]}.get(
            host, ["93.184.216.34"]
        )
    monkeypatch.setattr(crawl_pdf_mod, "_resolve", resolve)


def _transport(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        resp = routes.get(str(request.url))
        return resp if resp is not None else httpx.Response(404)

    t = httpx.MockTransport(handler)
    t.seen = seen  # type: ignore[attr-defined]
    return t


# ── extraction ──────────────────────────────────────────────


def test_extract_pdf_page_separated_markdown():
    out = crawl_pdf_mod.extract_pdf(PDF, url="https://x.test/a/plan.pdf")
    assert out["title"] == "Testplan"
    assert out["author"] == "Test Author"
    assert out["pages"] == 3
    assert out["pages_extracted"] == 3
    assert out["truncated"] is False
    md = out["markdown"]
    assert md.startswith("# Testplan\n")
    assert "## Page 1\n\nErste Seite mit Text." in md
    assert "## Page 3\n\nDritte Seite, Bewehrung nach DIN 1045." in md
    assert "## Page 2" not in md  # blank page leaves no empty section
    assert "\n\n---\n\n" in md


def test_extract_pdf_title_falls_back_to_url_filename():
    data = _make_pdf(["Inhalt ohne Metadaten."])
    out = crawl_pdf_mod.extract_pdf(data, url="https://x.test/docs/Leistungs%20Verzeichnis.pdf")
    assert out["title"] == "Leistungs Verzeichnis.pdf"


def test_extract_pdf_truncates_at_max_pages():
    data = _make_pdf([f"Seite {i}" for i in range(1, 6)])
    out = crawl_pdf_mod.extract_pdf(data, max_pages=2)
    assert out["pages"] == 5
    assert out["pages_extracted"] == 2
    assert out["truncated"] is True
    assert "## Page 2" in out["markdown"] and "## Page 3" not in out["markdown"]


def test_extract_pdf_truncates_at_max_chars():
    data = _make_pdf(["A" * 80, "B" * 80, "C" * 80])
    out = crawl_pdf_mod.extract_pdf(data, max_chars=60)
    assert out["truncated"] is True
    assert len(out["markdown"]) < 120


def test_extract_pdf_without_text_layer_is_a_clear_error():
    with pytest.raises(ValueError, match="no extractable text layer"):
        crawl_pdf_mod.extract_pdf(_make_pdf(["", ""]))


def test_extract_pdf_password_protected_is_a_clear_error():
    with pytest.raises(ValueError, match="password-protected"):
        crawl_pdf_mod.extract_pdf(_make_pdf(["geheim"], password="pw"))


def test_extract_pdf_garbage_is_a_clear_error():
    with pytest.raises(ValueError, match="cannot open PDF"):
        crawl_pdf_mod.extract_pdf(b"%PDF-1.7 not really")


# ── host guard ──────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a.pdf",
    "http://10.1.2.3/a.pdf",
    "http://169.254.169.254/latest/meta-data",
    "http://[::1]/a.pdf",
    "http://[::ffff:127.0.0.1]/a.pdf",
    "http://100.64.0.1/a.pdf",
    "http://internal.test/a.pdf",
    "http://rebind.test/a.pdf",  # one private answer is enough to refuse
    "ftp://93.184.216.34/a.pdf",
    "file:///etc/passwd",
])
async def test_assert_public_url_refuses_internal(url):
    with pytest.raises(crawl_pdf_mod.NotPublicHostError):
        await crawl_pdf_mod.assert_public_url(url)


async def test_assert_public_url_allows_public():
    await crawl_pdf_mod.assert_public_url("https://93.184.216.34/a.pdf")
    await crawl_pdf_mod.assert_public_url("https://www.example.test/a.pdf")


# ── fetch ───────────────────────────────────────────────────


async def test_fetch_detects_pdf_by_magic_not_content_type():
    t = _transport({"https://x.test/download?id=7": httpx.Response(
        200, content=PDF, headers={"content-type": "application/octet-stream"})})
    got = await crawl_pdf_mod.fetch_pdf_bytes("https://x.test/download?id=7", transport=t)
    assert got is not None
    data, final_url, ctype = got
    assert data == PDF and final_url == "https://x.test/download?id=7"
    assert ctype == "application/octet-stream"


async def test_fetch_returns_none_for_html_even_if_labelled_pdf():
    t = _transport({"https://x.test/a.pdf": httpx.Response(
        200, content=b"<html>" + b"x" * 4000, headers={"content-type": "application/pdf"})})
    assert await crawl_pdf_mod.fetch_pdf_bytes("https://x.test/a.pdf", transport=t) is None


async def test_fetch_returns_none_when_refused():
    t = _transport({"https://x.test/a.pdf": httpx.Response(403, content=b"blocked")})
    assert await crawl_pdf_mod.fetch_pdf_bytes("https://x.test/a.pdf", transport=t) is None


@pytest.mark.parametrize("status", [404, 410])
async def test_crawl_pdf_reports_a_gone_origin_instead_of_crawl4ais_500(status):
    t = _transport({"https://x.test/a.pdf": httpx.Response(status, content=b"nope")})
    out = await crawl_pdf_mod.crawl_pdf("https://x.test/a.pdf", transport=t)
    assert out == {
        "error": f"Crawl failed: URL returned HTTP {status}", "url": "https://x.test/a.pdf",
        "status_code": status,
    }


async def test_fetch_follows_redirects_to_public_hosts():
    t = _transport({
        "https://x.test/a.pdf": httpx.Response(302, headers={"location": "/files/a.pdf"}),
        "https://x.test/files/a.pdf": httpx.Response(200, content=PDF),
    })
    got = await crawl_pdf_mod.fetch_pdf_bytes("https://x.test/a.pdf", transport=t)
    assert got is not None and got[1] == "https://x.test/files/a.pdf"


async def test_fetch_refuses_redirect_into_internal_network():
    t = _transport({
        "https://x.test/a.pdf": httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data"}),
    })
    with pytest.raises(crawl_pdf_mod.NotPublicHostError):
        await crawl_pdf_mod.fetch_pdf_bytes("https://x.test/a.pdf", transport=t)
    assert t.seen == ["https://x.test/a.pdf"]  # the internal hop was never requested


async def test_fetch_caps_size_from_content_length():
    t = _transport({"https://x.test/a.pdf": httpx.Response(
        200, content=PDF, headers={"content-length": str(60 * 1024 * 1024)})})
    with pytest.raises(ValueError, match="over the 50 MB cap"):
        await crawl_pdf_mod.fetch_pdf_bytes("https://x.test/a.pdf", transport=t)


async def test_fetch_caps_size_while_streaming():
    t = _transport({"https://x.test/a.pdf": httpx.Response(200, content=PDF + b"\0" * 5000)})
    with pytest.raises(ValueError, match="cap"):
        await crawl_pdf_mod.fetch_pdf_bytes(
            "https://x.test/a.pdf", transport=t, max_bytes=len(PDF),
        )


async def test_crawl_pdf_returns_crawl4ai_shape():
    t = _transport({"https://x.test/plan.pdf": httpx.Response(
        200, content=PDF, headers={"content-type": "application/pdf"})})
    out = await crawl_pdf_mod.crawl_pdf("https://x.test/plan.pdf", transport=t)
    assert out["success"] is True and out["source"] == "pdf-direct"
    r = out["results"][0]
    assert r["success"] is True and r["status_code"] == 200
    assert r["url"] == "https://x.test/plan.pdf"
    assert r["metadata"]["source"] == "pdf-direct"
    assert r["metadata"]["pages"] == 3
    assert r["metadata"]["title"] == "Testplan"
    assert r["metadata"]["bytes"] == len(PDF)
    assert r["markdown"]["fit_markdown"] == r["markdown"]["raw_markdown"]
    assert "Bewehrung" in r["markdown"]["raw_markdown"]


async def test_crawl_pdf_none_for_internal_host_so_crawl4ai_keeps_the_call():
    assert await crawl_pdf_mod.crawl_pdf("http://internal.test/a.pdf") is None


async def test_crawl_pdf_scanned_pdf_is_an_error_dict():
    t = _transport({"https://x.test/scan.pdf": httpx.Response(200, content=_make_pdf([""]))})
    out = await crawl_pdf_mod.crawl_pdf("https://x.test/scan.pdf", transport=t)
    assert "no extractable text layer" in out["error"]
    assert out["url"] == "https://x.test/scan.pdf"


# ── routing in the shared client ────────────────────────────


_C4AI_OK = {"success": True, "results": [{
    "url": "https://x.test/page", "success": True, "status_code": 200,
    "metadata": {"title": "Seite"}, "markdown": {"fit_markdown": "Inhalt"},
}]}
_C4AI_500 = {"error": "Crawl failed: Crawl4AI /crawl returned HTTP 500", "url": "u"}


async def test_pdf_url_goes_direct_and_skips_crawl4ai():
    direct = {"success": True, "source": "pdf-direct", "results": [{}]}
    with (
        patch.object(crawl_client, "crawl_pdf", AsyncMock(return_value=direct)) as pdf,
        patch.object(crawl_client, "_crawl_sync", AsyncMock()) as c4ai,
    ):
        out = await crawl_client.crawl4ai_request("https://x.test/plan.PDF")
    assert out is direct
    pdf.assert_awaited_once()
    c4ai.assert_not_awaited()


async def test_pdf_url_that_is_not_a_pdf_falls_back_to_crawl4ai_once():
    with (
        patch.object(crawl_client, "crawl_pdf", AsyncMock(return_value=None)) as pdf,
        patch.object(crawl_client, "_crawl_sync", AsyncMock(return_value=_C4AI_500)) as c4ai,
    ):
        out = await crawl_client.crawl4ai_request("https://x.test/landing.pdf")
    assert out is _C4AI_500
    pdf.assert_awaited_once()
    c4ai.assert_awaited_once()


async def test_html_url_never_probes_for_pdf_when_crawl4ai_succeeds():
    with (
        patch.object(crawl_client, "crawl_pdf", AsyncMock()) as pdf,
        patch.object(crawl_client, "_crawl_sync", AsyncMock(return_value=_C4AI_OK)),
    ):
        out = await crawl_client.crawl4ai_request("https://x.test/page")
    assert out is _C4AI_OK
    pdf.assert_not_awaited()


async def test_failed_crawl_of_extensionless_pdf_is_rescued():
    direct = {"success": True, "source": "pdf-direct", "results": [{}]}
    with (
        patch.object(crawl_client, "crawl_pdf", AsyncMock(return_value=direct)) as pdf,
        patch.object(crawl_client, "_crawl_sync", AsyncMock(return_value=_C4AI_500)),
    ):
        out = await crawl_client.crawl4ai_request("https://arxiv.test/pdf/1706.03762")
    assert out is direct
    pdf.assert_awaited_once_with("https://arxiv.test/pdf/1706.03762")


async def test_failed_result_inside_success_envelope_is_also_probed():
    failed = {"success": True, "results": [{"success": False, "error_message": "blocked"}]}
    with (
        patch.object(crawl_client, "crawl_pdf", AsyncMock(return_value=None)) as pdf,
        patch.object(crawl_client, "_crawl_sync", AsyncMock(return_value=failed)),
    ):
        out = await crawl_client.crawl4ai_request("https://x.test/page")
    assert out is failed
    pdf.assert_awaited_once()


def test_crawl_error_surfaces_status_correlation_id_and_hint():
    req = httpx.Request("POST", "http://crawl4ai:11235/crawl")
    resp = httpx.Response(
        500, json={"error": "Internal server error", "correlation_id": "2356bd7ab909"},
        request=req,
    )
    err = crawl_client._crawl_error(
        httpx.HTTPStatusError("500", request=req, response=resp), "https://x.test", "/crawl",
    )
    assert err["error"] == (
        "Crawl failed: Crawl4AI /crawl returned HTTP 500 (Internal server error)"
    )
    assert err["status_code"] == 500
    assert err["correlation_id"] == "2356bd7ab909"
    assert "anti-bot" in err["hint"]


def test_crawl_error_400_has_detail_and_no_hint():
    req = httpx.Request("POST", "http://crawl4ai:11235/crawl")
    resp = httpx.Response(400, json={"detail": "Rejected config"}, request=req)
    err = crawl_client._crawl_error(
        httpx.HTTPStatusError("400", request=req, response=resp), "https://x.test", "/crawl",
    )
    assert "HTTP 400 (Rejected config)" in err["error"]
    assert "hint" not in err


def _patch_c4ai_transport(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(crawl_client.httpx, "AsyncClient", factory)


async def test_job_api_500_falls_back_to_synchronous_crawl(monkeypatch):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/crawl/job":
            return httpx.Response(500, json={"error": "Internal server error"})
        return httpx.Response(200, json=_C4AI_OK)

    _patch_c4ai_transport(monkeypatch, handler)
    out = await crawl_client.crawl4ai_job("https://x.test/page", poll_interval=0)
    assert out == _C4AI_OK
    assert calls == ["/crawl/job", "/crawl"]


async def test_job_api_400_is_not_masked(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad config"})

    _patch_c4ai_transport(monkeypatch, handler)
    out = await crawl_client.crawl4ai_job("https://x.test/page", poll_interval=0)
    assert out["error"].startswith("Crawl job failed: Crawl4AI /crawl/job returned HTTP 400")


# ── the MCP tools end to end ────────────────────────────────


async def test_crawl_page_returns_pdf_markdown_and_metadata():
    t = _transport({"https://x.test/plan.pdf": httpx.Response(200, content=PDF)})
    with (
        patch.object(crawl_client, "crawl_pdf", functools.partial(crawl_pdf_mod.crawl_pdf, transport=t)),
        patch.object(crawl_client, "_crawl_sync", AsyncMock(side_effect=AssertionError("no Crawl4AI"))),
    ):
        out = await crawl_page("https://x.test/plan.pdf")
    assert out["title"] == "Testplan"
    assert out["status_code"] == 200
    assert out["source"] == "pdf-direct"
    assert out["pages"] == 3 and out["pages_extracted"] == 3 and out["truncated"] is False
    assert "## Page 3" in out["markdown"]


async def test_crawl_and_store_tags_pdf_and_chunks_the_text():
    page = {
        "url": "https://x.test/plan.pdf", "status_code": 200, "title": "Testplan",
        "source": "pdf-direct", "pages": 3, "markdown": "# Testplan\n\n" + "Text " * 50,
    }
    enqueue = AsyncMock(return_value={"status": "queued", "queue_ids": [1], "chunks": 1,
                                      "document_id": None})
    with (
        patch.object(mcp_server, "crawl_page", AsyncMock(return_value=page)),
        patch("nobrainr.db.write_queue.enqueue_document_chunks", enqueue),
        patch.object(settings, "interest_tracking_enabled", False),
    ):
        out = await crawl_and_store("https://x.test/plan.pdf", tags=["norm"], max_content_chars=100)
    assert out["title"] == "Testplan" and out["chars_total"] == 100
    kwargs = enqueue.await_args.kwargs
    assert kwargs["tags"] == ["norm", "crawled", "pdf"]
    assert kwargs["source_ref"] == "https://x.test/plan.pdf"
