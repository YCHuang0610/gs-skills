#!/usr/bin/env python3
"""Shared Google Scholar skill helpers adapted from literature-harvester.

The helpers here intentionally avoid shadow-library behavior. They resolve
metadata and legally accessible full text, then classify blocked/login/captcha
responses conservatively.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
import re
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

LOGIN_PATTERNS = (
    "sign in",
    "signin",
    "log in",
    "login",
    "institutional access",
    "shibboleth",
    "single sign-on",
    "sso",
    "subscription",
    "subscribe",
    "purchase",
    "access denied",
    "captcha",
    "verify you are human",
    "robot check",
    "unusual traffic",
)


@dataclass
class HttpResponse:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    body: bytes


def normalize_doi(value: Any) -> str:
    if not value:
        return ""
    doi = str(value).strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    return doi.strip().lower()


def extract_doi(value: Any) -> str:
    text = str(value or "")
    match = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, flags=re.I)
    return normalize_doi(match.group(0)) if match else normalize_doi(text)


def strip_tags(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def safe_filename(value: str, fallback: str = "paper") -> str:
    value = normalize_doi(value) or value or fallback
    value = re.sub(r"[^\w.\-]+", "_", value, flags=re.UNICODE).strip("._")
    return value[:120] or fallback


def http_get(
    url: str,
    *,
    accept: str = "*/*",
    timeout: int = 30,
    headers: dict[str, str] | None = None,
    retries: int = 2,
    retry_delay: float = 1.0,
) -> HttpResponse:
    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
    }
    if headers:
        req_headers.update(headers)
    retry_statuses = {429, 500, 502, 503, 504}
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return HttpResponse(
                    url=url,
                    final_url=resp.geturl(),
                    status=getattr(resp, "status", 200),
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    body=resp.read(),
                )
        except urllib.error.HTTPError as exc:
            body = exc.read() if exc.fp else b""
            response = HttpResponse(
                url=url,
                final_url=exc.geturl() or url,
                status=exc.code,
                headers={k.lower(): v for k, v in exc.headers.items()},
                body=body,
            )
            if exc.code in retry_statuses and attempt < retries:
                time.sleep(retry_delay * (attempt + 1))
                continue
            return response
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(retry_delay * (attempt + 1))
                continue
            raise last_error
    raise RuntimeError(f"unreachable HTTP retry state for {url}")


def is_trusted_oa_article_page(url: str, text: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    trusted_hosts = (
        "pmc.ncbi.nlm.nih.gov",
        "www.ncbi.nlm.nih.gov",
        "europepmc.org",
        "www.frontiersin.org",
        "frontiersin.org",
    )
    if not any(host == h or host.endswith("." + h) for h in trusted_hosts):
        return False
    article_markers = (
        "pmcid:",
        "pubmed central",
        "article",
        "abstract",
        "references",
        "full text",
        "download pdf",
        "journal article",
    )
    return sum(1 for marker in article_markers if marker in text) >= 2


def looks_like_login_page(body: bytes, content_type: str, url: str = "") -> bool:
    if not body:
        return True
    text = body[:50000].decode("utf-8", errors="ignore").lower()
    if is_trusted_oa_article_page(url, text):
        return False
    if any(pattern in text for pattern in LOGIN_PATTERNS):
        return True
    if "html" in content_type and len(body) < 2000:
        if "refresh" in text or "location.href" in text or "window.location" in text:
            return True
    return False


def validate_download(resp: HttpResponse, expected: str) -> tuple[bool, str, str]:
    ctype = resp.headers.get("content-type", "").lower()
    if resp.status in {401, 402, 403, 407, 429}:
        return False, "failed_needs_manual_access", f"HTTP {resp.status}"
    if resp.status >= 400:
        return False, "failed_http", f"HTTP {resp.status}"
    body = resp.body
    if expected == "pdf":
        if body.startswith(b"%PDF") and len(body) > 8000:
            return True, "ok", "valid PDF"
        if "pdf" in ctype and len(body) > 8000 and not looks_like_login_page(body, ctype, resp.final_url):
            return True, "ok", "PDF-like response"
        if looks_like_login_page(body, ctype, resp.final_url):
            return False, "failed_needs_manual_access", "login/subscription/captcha-like page"
        return False, "failed_invalid_pdf", "response is not a valid PDF"
    if expected in {"html", "xml"}:
        if looks_like_login_page(body, ctype, resp.final_url):
            return False, "failed_needs_manual_access", "login/subscription/captcha-like page"
        if expected == "xml" and ("xml" in ctype or body.lstrip().startswith(b"<?xml") or b"<article" in body[:2000]):
            return True, "ok", "valid XML"
        if expected == "html" and ("html" in ctype or b"<html" in body[:2000].lower()) and len(body) > 2000:
            return True, "ok", "valid HTML"
        return False, f"failed_invalid_{expected}", f"response is not valid {expected.upper()}"
    if looks_like_login_page(body, ctype, resp.final_url):
        return False, "failed_needs_manual_access", "login/subscription/captcha-like page"
    return len(body) > 2000, "ok" if len(body) > 2000 else "failed_too_small", "generic response"


def infer_kind(url: str, preferred: str = "") -> str:
    if preferred:
        return preferred
    lower = url.lower().split("?", 1)[0]
    if lower.endswith(".pdf") or "/pdf" in lower:
        return "pdf"
    if lower.endswith(".xml") or "oai.cgi" in lower:
        return "xml"
    return "html"


def enrich_unpaywall(record: dict[str, Any], email: str | None, timeout: int = 30) -> dict[str, Any]:
    doi = normalize_doi(record.get("doi"))
    if not doi or not email:
        return record
    url = f"https://api.unpaywall.org/v2/{urllib.parse.quote(doi)}?" + urllib.parse.urlencode(
        {"email": email}
    )
    try:
        resp = http_get(url, accept="application/json,*/*", timeout=timeout)
        ok, _, message = validate_download(resp, "json")
        if not ok and resp.status >= 400:
            record["unpaywall_error"] = message
            return record
        data = json.loads(resp.body.decode("utf-8"))
    except Exception as exc:
        record["unpaywall_error"] = str(exc)
        return record
    record["is_oa"] = bool(data.get("is_oa"))
    record["oa_status"] = data.get("oa_status", "")
    best = data.get("best_oa_location") or {}
    if best.get("url_for_pdf") and not record.get("pdfUrl") and not record.get("pdf_url"):
        record["pdfUrl"] = best["url_for_pdf"]
    if best.get("url_for_landing_page") and not record.get("html_url"):
        record["html_url"] = best["url_for_landing_page"]
    return record


def collect_oa_urls(record: dict[str, Any]) -> list[tuple[str, str]]:
    urls: list[tuple[str, str]] = []

    pmcid = str(record.get("pmcid") or "").upper()
    if pmcid and not pmcid.startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    if pmcid.startswith("PMC"):
        urls.append((f"https://europepmc.org/articles/{pmcid}?pdf=render", "pdf"))
        urls.append((f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/pdf/", "pdf"))

    for field, kind in [
        ("pdfUrl", "pdf"),
        ("fullTextUrl", "pdf"),
        ("pdf_url", "pdf"),
        ("xml_url", "xml"),
        ("html_url", "html"),
        ("url", ""),
        ("landing_url", ""),
    ]:
        value = record.get(field)
        if value:
            item = (str(value), kind or infer_kind(str(value)))
            if item not in urls:
                urls.append(item)

    arxiv_id = record.get("arxiv_id") or extract_arxiv_id(record.get("url", "")) or extract_arxiv_id(record.get("pdfUrl", ""))
    if arxiv_id and not any("arxiv.org/pdf" in u for u, _ in urls):
        urls.append((f"https://arxiv.org/pdf/{arxiv_id}", "pdf"))

    return urls


def extract_arxiv_id(value: Any) -> str:
    text = str(value or "")
    match = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})(?:v\d+)?", text, flags=re.I)
    return match.group(1) if match else ""
