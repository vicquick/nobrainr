"""Direct PDF fetch + text extraction for the crawl path.

Crawl4AI 0.9 fetches and scrapes a PDF fine, then rejects its own result with
"Blocked by anti-bot protection: Cloudflare JS challenge" — its challenge
detector misreads the PDF byte stream — and answers the whole request with a
bare HTTP 500. Every PDF crawl failed that way. This module bypasses the
browser for PDFs: fetch the bytes over plain HTTP, extract the text layer with
pymupdf (already a dependency, used by importers/documents.py), and hand back
a result in Crawl4AI's own shape so no caller has to know the difference.

The fetch runs from inside nobrainr's network, next to postgres and
llama-server, so it refuses every host that does not resolve to a public
address — re-checked on each redirect hop. Internal URLs still go to Crawl4AI
exactly as before; this path never widens what nobrainr can reach.
"""

import asyncio
import ipaddress
import logging
import re
import socket
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlparse

import httpx

logger = logging.getLogger("nobrainr")

PDF_SOURCE = "pdf-direct"
PDF_MAX_BYTES = 50 * 1024 * 1024
PDF_MAX_PAGES = 500
PDF_MAX_CHARS = 1_000_000
PDF_TIMEOUT = 60.0
PDF_MAX_REDIRECTS = 5
# The PDF spec lets a header sit anywhere in the first 1024 bytes.
_MAGIC = b"%PDF-"
_MAGIC_WINDOW = 1024

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; nobrainr/0.2; +https://github.com/vicquick/nobrainr)",
    "Accept": "application/pdf,*/*;q=0.8",
}


class NotPublicHostError(Exception):
    """The URL points at a private, loopback, link-local or unresolvable host."""


class OriginGoneError(Exception):
    """The origin answered 404/410 — no browser would fare better, so say so."""

    def __init__(self, status: int):
        super().__init__(f"URL returned HTTP {status}")
        self.status = status


def is_pdf_path(url: str) -> bool:
    """True when the URL path ends in .pdf — the cheap pre-check before any request."""
    return urlparse(url).path.lower().endswith(".pdf")


def _ip_is_public(raw: str) -> bool:
    ip = ipaddress.ip_address(raw.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


async def _resolve(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def assert_public_url(url: str) -> None:
    """Raise NotPublicHostError unless every address the host resolves to is public."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise NotPublicHostError(f"unsupported URL: {url}")
    host = parsed.hostname
    try:
        addrs = [host] if _is_ip_literal(host) else await _resolve(host)
    except OSError as e:
        raise NotPublicHostError(f"cannot resolve {host}: {e}") from e
    if not addrs or not all(_ip_is_public(a) for a in addrs):
        raise NotPublicHostError(f"{host} does not resolve to a public address")


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return True
    except ValueError:
        return False


async def fetch_pdf_bytes(
    url: str,
    *,
    max_bytes: int = PDF_MAX_BYTES,
    timeout: float = PDF_TIMEOUT,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bytes, str, str] | None:
    """Download url if — and only if — it is a PDF.

    Returns (data, final_url, content_type), or None when the response is not a
    PDF (decided by the %PDF- magic, not by the Content-Type header, which
    servers get wrong in both directions) or the server refused us.

    Raises NotPublicHostError for internal hosts, OriginGoneError on 404/410
    and ValueError when the PDF exceeds max_bytes.
    """
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, headers=_HEADERS, transport=transport,
    ) as client:
        current = url
        for _hop in range(PDF_MAX_REDIRECTS + 1):
            await assert_public_url(current)
            resp = await client.send(client.build_request("GET", current), stream=True)
            try:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        return None
                    current = urljoin(str(resp.url), location)
                    continue
                if resp.status_code in (404, 410):
                    raise OriginGoneError(resp.status_code)
                if resp.status_code >= 400:
                    logger.info("PDF probe for %s got HTTP %d", url, resp.status_code)
                    return None

                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ValueError(
                        f"PDF is {int(declared) // (1024 * 1024)} MB, over the "
                        f"{max_bytes // (1024 * 1024)} MB cap"
                    )

                buf = bytearray()
                checked = False
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if not checked and len(buf) >= _MAGIC_WINDOW:
                        if _MAGIC not in buf[:_MAGIC_WINDOW]:
                            return None
                        checked = True
                    if len(buf) > max_bytes:
                        raise ValueError(
                            f"PDF exceeds the {max_bytes // (1024 * 1024)} MB cap"
                        )
                if not checked and _MAGIC not in buf[:_MAGIC_WINDOW]:
                    return None
                return bytes(buf), str(resp.url), resp.headers.get("content-type", "")
            finally:
                await resp.aclose()
        logger.info("PDF probe for %s: too many redirects", url)
        return None


def _title_from_url(url: str) -> str:
    name = unquote(PurePosixPath(urlparse(url).path).name)
    return name or url


def _clean_page_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pdf(
    data: bytes,
    *,
    url: str = "",
    max_pages: int = PDF_MAX_PAGES,
    max_chars: int = PDF_MAX_CHARS,
) -> dict:
    """Extract the text layer of a PDF into page-separated markdown.

    Returns {"markdown", "title", "pages", "pages_extracted", "truncated",
    "author"}. Raises ValueError for unreadable, encrypted or text-less PDFs.
    """
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise ValueError(f"cannot open PDF: {e}") from e

    try:
        if doc.needs_pass and not doc.authenticate(""):
            raise ValueError("PDF is password-protected")

        meta = doc.metadata or {}
        title = (meta.get("title") or "").strip() or _title_from_url(url)
        total = doc.page_count
        flags = pymupdf.TEXTFLAGS_TEXT | pymupdf.TEXT_DEHYPHENATE

        sections: list[str] = []
        chars = 0
        extracted = 0
        truncated = False
        for index in range(total):
            if index >= max_pages or chars >= max_chars:
                truncated = True
                break
            text = _clean_page_text(doc[index].get_text("text", flags=flags))
            extracted += 1
            if not text:
                continue
            section = f"## Page {index + 1}\n\n{text}"
            sections.append(section)
            chars += len(section)
    finally:
        doc.close()

    if not sections:
        raise ValueError(
            "PDF has no extractable text layer (scanned?) — "
            "memory_import_documents can OCR it with the vision model"
        )

    body = "\n\n---\n\n".join(sections)
    if len(body) > max_chars:
        body = body[:max_chars]
        truncated = True

    return {
        "markdown": f"# {title}\n\n{body}\n",
        "title": title,
        "pages": total,
        "pages_extracted": extracted,
        "truncated": truncated,
        "author": (meta.get("author") or "").strip() or None,
    }


async def crawl_pdf(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    """Crawl a PDF URL without Crawl4AI.

    Returns None when the URL is not a PDF or may not be fetched directly
    (internal host, refused, network error) — the caller then keeps its
    Crawl4AI result. Otherwise returns either an error dict ({"error", "url"})
    for a PDF that cannot be read or a URL the origin reports as 404/410
    (Crawl4AI would only have masked that as a 500), or a success dict in Crawl4AI's shape:
    {"success": True, "results": [{"url", "success", "status_code",
    "metadata": {..., "source": "pdf-direct", "pages"}, "markdown":
    {"raw_markdown", "fit_markdown"}, "links"}]}.
    """
    try:
        fetched = await fetch_pdf_bytes(url, transport=transport)
    except NotPublicHostError as e:
        logger.info("PDF direct fetch skipped for %s: %s", url, e)
        return None
    except OriginGoneError as e:
        return {"error": f"Crawl failed: {e}", "url": url, "status_code": e.status}
    except ValueError as e:
        return {"error": f"PDF fetch failed: {e}", "url": url, "source": PDF_SOURCE}
    except httpx.HTTPError as e:
        logger.info("PDF direct fetch failed for %s: %s", url, e)
        return None
    if fetched is None:
        return None

    data, final_url, content_type = fetched
    try:
        pdf = await asyncio.to_thread(extract_pdf, data, url=final_url)
    except ValueError as e:
        return {"error": f"PDF extraction failed: {e}", "url": url, "source": PDF_SOURCE}

    logger.info(
        "PDF direct: %s — %d/%d pages, %d chars",
        final_url, pdf["pages_extracted"], pdf["pages"], len(pdf["markdown"]),
    )
    metadata = {
        "title": pdf["title"],
        "source": PDF_SOURCE,
        "pages": pdf["pages"],
        "pages_extracted": pdf["pages_extracted"],
        "truncated": pdf["truncated"],
        "content_type": content_type,
        "bytes": len(data),
    }
    if pdf["author"]:
        metadata["author"] = pdf["author"]
    return {
        "success": True,
        "source": PDF_SOURCE,
        "results": [{
            "url": final_url,
            "success": True,
            "status_code": 200,
            "metadata": metadata,
            "markdown": {"raw_markdown": pdf["markdown"], "fit_markdown": pdf["markdown"]},
            "links": {"internal": [], "external": []},
        }],
    }
