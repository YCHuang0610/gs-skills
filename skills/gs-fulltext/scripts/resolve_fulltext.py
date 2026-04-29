#!/usr/bin/env python3
"""Resolve OA-oriented full-text links for a Google Scholar result JSON object."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


GS_EXPORT_SCRIPTS = Path.home() / ".codex" / "skills" / "gs-export" / "scripts"
sys.path.insert(0, str(GS_EXPORT_SCRIPTS))

from gs_lit_utils import collect_oa_urls, enrich_unpaywall, extract_doi, normalize_doi  # noqa: E402


def normalize_record(record: dict) -> dict:
    doi = normalize_doi(record.get("doi")) or extract_doi(record.get("paperUrl") or record.get("href") or record.get("url"))
    out = dict(record)
    if doi:
        out["doi"] = doi
    if record.get("fullTextUrl") and not out.get("pdfUrl"):
        out["pdfUrl"] = record["fullTextUrl"]
    if record.get("paperUrl") and not out.get("url"):
        out["url"] = record["paperUrl"]
    if record.get("href") and not out.get("url"):
        out["url"] = record["href"]
    return out


def resolve(record: dict, email: str | None = None) -> dict:
    record = normalize_record(record)
    if email:
        record = enrich_unpaywall(record, email)

    links = []
    seen = set()
    for url, kind in collect_oa_urls(record):
        if not url or url in seen:
            continue
        seen.add(url)
        links.append({"kind": kind, "url": url})

    doi = normalize_doi(record.get("doi"))
    return {
        "title": record.get("title", ""),
        "doi": doi,
        "doiUrl": f"https://doi.org/{doi}" if doi else "",
        "publisher": record.get("url") or record.get("paperUrl") or record.get("href") or "",
        "is_oa": record.get("is_oa", ""),
        "oa_status": record.get("oa_status", ""),
        "links": links,
        "unpaywall_error": record.get("unpaywall_error", ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve full-text links for a Google Scholar result JSON object")
    parser.add_argument("--input", "-i", help="JSON file; defaults to stdin")
    parser.add_argument("--email", help="Email for Unpaywall DOI lookup")
    args = parser.parse_args(argv)

    if args.input:
        with open(args.input, "r", encoding="utf-8") as f:
            record = json.load(f)
    else:
        record = json.load(sys.stdin)

    print(json.dumps(resolve(record, args.email), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
