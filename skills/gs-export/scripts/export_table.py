#!/usr/bin/env python3
"""Export Google Scholar result JSON/JSONL to Chinese/English candidate tables."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from gs_lit_utils import extract_doi, normalize_doi


EXPORT_FIELDS = [
    ("priority_zh", "优先级"),
    ("priority", "Priority"),
    ("title", "题名/Title"),
    ("authors", "作者/Authors"),
    ("year", "年份/Year"),
    ("journal", "期刊/Journal"),
    ("doi", "DOI"),
    ("pmid", "PMID"),
    ("pmcid", "PMCID"),
    ("arxiv_id", "arXiv ID"),
    ("abstract", "摘要/Abstract"),
    ("publication_type", "文献类型/Type"),
    ("source_apis", "来源数据库/Sources"),
    ("citation_count", "引用数/Citations"),
    ("is_oa", "是否OA/Is OA"),
    ("pdf_url", "PDF链接/PDF URL"),
    ("html_url", "HTML链接/HTML URL"),
    ("xml_url", "XML链接/XML URL"),
    ("landing_url", "入口页/Landing URL"),
    ("fulltext_status", "全文状态/Fulltext Status"),
    ("access_source", "获取来源/Access Source"),
    ("journal_metrics", "期刊指标/Journal Metrics"),
    ("relevance_score", "相关性分数/Relevance Score"),
    ("reason_zh", "中文理由"),
    ("reason_en", "English Reason"),
    ("notes", "备注/Notes"),
]


def csv_value(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def read_records(path: str) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def normalize_record(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    meta = str(out.get("journalYear") or "")
    if not out.get("journal") and meta:
        out["journal"] = meta.split(",", 1)[0].strip()
    if not out.get("year"):
        import re

        match = re.search(r"\b(19|20)\d{2}\b", meta)
        if match:
            out["year"] = match.group(0)
    if out.get("citedBy") and not out.get("citation_count"):
        out["citation_count"] = out["citedBy"]
    if out.get("snippet") and not out.get("abstract"):
        out["abstract"] = out["snippet"]
    if out.get("fullTextUrl") and not out.get("pdf_url"):
        out["pdf_url"] = out["fullTextUrl"]
    if out.get("href") and not out.get("landing_url"):
        out["landing_url"] = out["href"]
    if out.get("paperUrl") and not out.get("landing_url"):
        out["landing_url"] = out["paperUrl"]
    if not out.get("doi"):
        out["doi"] = extract_doi(out.get("landing_url", ""))
    else:
        out["doi"] = normalize_doi(out["doi"])
    out.setdefault("source_apis", ["google_scholar"])
    if out.get("pdf_url"):
        out.setdefault("fulltext_status", "has_fulltext_link")
        out.setdefault("access_source", "google_scholar")
    return out


def export(records: list[dict[str, Any]], out_dir: str) -> tuple[Path, Path]:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    rows = [normalize_record(row) for row in records]
    table_path = out_path / "候选文献总表.csv"
    with table_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[label for _, label in EXPORT_FIELDS])
        writer.writeheader()
        for row in rows:
            writer.writerow({label: csv_value(row.get(field, "")) for field, label in EXPORT_FIELDS})

    missing_path = out_path / "missing_fulltext.csv"
    with missing_path.open("w", encoding="utf-8-sig", newline="") as f:
        fieldnames = ["DOI", "Title", "Status", "Next Step"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            status = row.get("fulltext_status", "")
            if status not in {"downloaded", "has_fulltext_link"}:
                writer.writerow(
                    {
                        "DOI": row.get("doi", ""),
                        "Title": row.get("title", ""),
                        "Status": status or "not_downloaded",
                        "Next Step": "Use OA lookup, institutional login/VPN, library document delivery, author request, or OA preprint search.",
                    }
                )
    return table_path, missing_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Google Scholar results to CSV tables")
    parser.add_argument("--input", required=True, help="JSON array or JSONL of Google Scholar result objects")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)

    table_path, missing_path = export(read_records(args.input), args.out_dir)
    print(f"Wrote {table_path}")
    print(f"Wrote {missing_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
