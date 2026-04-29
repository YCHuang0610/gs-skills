#!/usr/bin/env python3
"""Push PubMed/Google Scholar paper data to Zotero via local Connector API.

Three-step flow (Zotero 7.x saveItems ignores attachments field):
1. saveItems      — save metadata
2. download_pdf   — download PDF binary via Python urllib
3. saveAttachment — upload PDF binary to Zotero, linked to parent item

Session strategy: deterministic sessionID derived from content hash.
- 201 = saved successfully
- 409 = SESSION_EXISTS = already saved (idempotent, treat as success)
"""

import json
import sys
import io
import hashlib
import urllib.request
import urllib.error
import re
import os
from datetime import datetime, timezone

from gs_lit_utils import (
    collect_oa_urls,
    enrich_unpaywall,
    extract_doi,
    http_get,
    normalize_doi,
    validate_download,
)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

ZOTERO_API = 'http://127.0.0.1:23119/connector'
HTTP_TIMEOUT = 15
PDF_DOWNLOAD_TIMEOUT = 60


def zotero_request(endpoint, data=None, timeout=HTTP_TIMEOUT):
    """Send request to Zotero local API with timeout."""
    url = f'{ZOTERO_API}/{endpoint}'
    body = json.dumps(data or {}, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers={
        'Content-Type': 'application/json',
        'X-Zotero-Connector-API-Version': '3'
    })
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        text = resp.read().decode('utf-8')
        return resp.status, json.loads(text) if text else None
    except urllib.error.HTTPError as e:
        resp_body = e.read().decode('utf-8', errors='replace')
        try:
            return e.code, json.loads(resp_body) if resp_body else None
        except json.JSONDecodeError:
            return e.code, {'error': resp_body}
    except urllib.error.URLError:
        return 0, None
    except TimeoutError:
        return -1, {'error': f'Request timeout ({timeout}s)'}


def make_session_id(items):
    """Generate deterministic sessionID from item content (titles hash)."""
    key = '|'.join(sorted(item.get('title', '') for item in items))
    return hashlib.md5(key.encode('utf-8', errors='surrogateescape')).hexdigest()[:12]


def get_selected_collection():
    """Get currently selected Zotero collection."""
    status, data = zotero_request('getSelectedCollection')
    if status != 200 or not data:
        return None
    return data


def list_collections():
    """List all available Zotero collections."""
    data = get_selected_collection()
    if not data:
        print('Error: Cannot connect to Zotero. Please ensure Zotero desktop is running.')
        return
    print(f'Current collection: {data.get("name", "?")} (ID: {data.get("id", "?")})')
    print(f'Library: {data.get("libraryName", "?")}')
    print()
    print('Available collections:')
    for t in data.get('targets', []):
        indent = '  ' * t.get('level', 0)
        recent = ' *' if t.get('recent') else ''
        print(f'  {indent}{t["name"]} (ID: {t["id"]}){recent}')


def parse_pubmed_authors(author_str):
    """Parse PubMed author string into Zotero creator list.

    PubMed format: "LastName Initials" e.g. "Váša F, Mišić B"
    or "LastName ForeName" e.g. "Smith John A"
    """
    if not author_str:
        return []
    authors = []
    for name in re.split(r',\s*', author_str):
        name = name.strip()
        if not name:
            continue
        parts = name.split(' ', 1)
        if len(parts) == 2:
            authors.append({
                'lastName': parts[0],
                'firstName': parts[1],
                'creatorType': 'author'
            })
        else:
            authors.append({
                'name': name,
                'creatorType': 'author'
            })
    return authors


def build_zotero_item(paper):
    """Build Zotero item JSON from PubMed paper data."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    doi = normalize_doi(paper.get('doi')) or extract_doi(paper.get('url', ''))

    # Parse authors
    if isinstance(paper.get('authors'), list) and paper['authors']:
        if isinstance(paper['authors'][0], dict):
            creators = []
            for a in paper['authors']:
                if 'lastName' in a:
                    creators.append({
                        'lastName': a['lastName'],
                        'firstName': a.get('firstName', a.get('initials', '')),
                        'creatorType': 'author'
                    })
                elif 'name' in a:
                    parts = a['name'].split(' ', 1)
                    if len(parts) == 2:
                        creators.append({
                            'lastName': parts[0],
                            'firstName': parts[1],
                            'creatorType': 'author'
                        })
                    else:
                        creators.append({
                            'name': a['name'],
                            'creatorType': 'author'
                        })
        else:
            creators = parse_pubmed_authors(', '.join(paper['authors']))
    elif isinstance(paper.get('authors'), str):
        creators = parse_pubmed_authors(paper['authors'])
    else:
        creators = []

    item = {
        'itemType': 'journalArticle',
        'title': paper.get('title', ''),
        'abstractNote': paper.get('abstract', ''),
        'date': paper.get('pubDate') or paper.get('pubdate', ''),
        'language': paper.get('language', 'en'),
        'libraryCatalog': paper.get('libraryCatalog', 'PubMed'),
        'accessDate': now,
        'volume': paper.get('volume', ''),
        'pages': paper.get('pages', ''),
        'publicationTitle': paper.get('journal') or paper.get('fulljournalname', ''),
        'journalAbbreviation': paper.get('journalAbbr') or paper.get('source', ''),
        'issue': paper.get('issue', ''),
        'DOI': doi,
        'url': f'https://pubmed.ncbi.nlm.nih.gov/{paper["pmid"]}/' if paper.get('pmid') else paper.get('url', ''),
        'creators': creators,
        'tags': [{'tag': k, 'type': 1} for k in paper.get('keywords', [])],
        'attachments': [],
    }

    # ISSN
    if paper.get('issn'):
        item['ISSN'] = paper['issn']

    # Extra field with PMID and other metadata
    extra_parts = []
    if paper.get('pmid'):
        extra_parts.append(f'PMID: {paper["pmid"]}')
    if paper.get('pmcid'):
        extra_parts.append(f'PMCID: {paper["pmcid"]}')
    if paper.get('pubtype'):
        pub_types = paper['pubtype'] if isinstance(paper['pubtype'], str) else ', '.join(paper['pubtype'])
        extra_parts.append(f'Publication Type: {pub_types}')
    if paper.get('dataCid'):
        extra_parts.append(f'Google Scholar data-cid: {paper["dataCid"]}')
    if paper.get('publisher'):
        extra_parts.append(f'Publisher: {paper["publisher"]}')
    if extra_parts:
        item['extra'] = '\n'.join(extra_parts)

    return item


def save_items(items, uri=''):
    """Push items to Zotero via saveItems API. Returns (status, msg, session_id).

    Uses deterministic sessionID (content hash) for idempotency:
    - 201 = saved successfully
    - 409 = same items already saved in this Zotero session (success)
    """
    session_id = make_session_id(items)

    for i, item in enumerate(items):
        if 'id' not in item:
            item['id'] = f'pm_{session_id}_{i}'

    data = {
        'sessionID': session_id,
        'uri': uri,
        'items': items
    }
    status, resp = zotero_request('saveItems', data)

    if status == 201:
        msg = f'Saved successfully (session: {session_id})'
    elif status == 409:
        msg = f'Already saved, no duplicate added (session: {session_id})'
    elif status == 500:
        detail = resp.get('error', '') if resp else ''
        if 'libraryEditable' in str(resp):
            return 500, 'Target library is read-only.', session_id
        return 500, f'Zotero internal error: {detail}', session_id
    elif status == 0:
        return 0, 'Zotero is not running or connection refused.', session_id
    elif status == -1:
        return -1, f'Request timeout ({HTTP_TIMEOUT}s)', session_id
    else:
        return status, f'Unknown error, HTTP {status}', session_id

    return 201, msg, session_id


def resolve_pdf_urls(paper):
    """Get candidate legally accessible PDF URLs from paper data."""
    email = paper.get('unpaywallEmail') or os.environ.get('GS_UNPAYWALL_EMAIL') or os.environ.get('UNPAYWALL_EMAIL')
    if email:
        paper = enrich_unpaywall(dict(paper), email)
    candidates = []
    seen = set()
    for url, kind in collect_oa_urls(paper):
        if kind != 'pdf' or not url or url in seen:
            continue
        seen.add(url)
        candidates.append(url)
    return candidates


def download_pdf(pdf_url, timeout=PDF_DOWNLOAD_TIMEOUT):
    """Download PDF from URL. Returns (bytes, error_msg|None)."""
    try:
        resp = http_get(pdf_url, accept='application/pdf,*/*', timeout=timeout)
        ok, status, message = validate_download(resp, 'pdf')
        if not ok:
            return None, f'{status}: {message}'
        return resp.body, None
    except urllib.error.HTTPError as e:
        return None, f'HTTP {e.code}'
    except urllib.error.URLError as e:
        return None, f'URL error: {e.reason}'
    except TimeoutError:
        return None, f'Download timeout ({timeout}s)'
    except Exception as e:
        return None, str(e)


def save_attachment(session_id, parent_item_id, pdf_bytes, pdf_url,
                    title='Full Text PDF'):
    """Upload PDF binary to Zotero via /connector/saveAttachment.

    Body = raw PDF bytes. Metadata in X-Metadata header.
    parentItemID links the attachment to the parent item from saveItems.
    sessionID must match the saveItems session.
    """
    metadata = json.dumps({
        'id': parent_item_id + '_pdf',
        'parentItemID': parent_item_id,
        'title': title,
        'url': pdf_url,
        'contentType': 'application/pdf',
    }, ensure_ascii=False)

    url = f'{ZOTERO_API}/saveAttachment?sessionID={session_id}'
    req = urllib.request.Request(url, data=pdf_bytes, headers={
        'Content-Type': 'application/pdf',
        'X-Metadata': metadata,
        'Content-Length': str(len(pdf_bytes)),
    })

    try:
        resp = urllib.request.urlopen(req, timeout=60)
        return resp.status, resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', errors='replace')
    except urllib.error.URLError:
        return 0, 'Connection refused'
    except TimeoutError:
        return -1, 'Timeout'


def main():
    """Main entry point. Accepts JSON paper data from file argument."""
    if len(sys.argv) > 1 and sys.argv[1] == '--list':
        list_collections()
        return

    # Check Zotero is running
    status, _ = zotero_request('ping')
    if status == 0:
        print('Error: Zotero is not running. Please start Zotero desktop.')
        sys.exit(1)

    # Show current collection
    col = get_selected_collection()
    if col:
        print(f'Zotero collection: {col.get("name", "?")}')

    # Read paper data from file argument
    if len(sys.argv) > 1 and sys.argv[1] != '--list':
        with open(sys.argv[1], 'r', encoding='utf-8') as f:
            paper_data = json.load(f)
    else:
        paper_data = json.load(sys.stdin)

    # Handle both single paper and array
    if isinstance(paper_data, list):
        papers = paper_data
    elif 'items' in paper_data:
        status, msg, _ = save_items(paper_data['items'], paper_data.get('uri', ''))
        if status == 201:
            print(f'Success: {msg} ({len(paper_data["items"])} papers)')
        else:
            print(f'Failed: {msg}')
            sys.exit(1)
        return
    else:
        papers = [paper_data]

    # Build Zotero items
    items = []
    for p in papers:
        if 'itemType' in p:
            items.append(p)
        else:
            items.append(build_zotero_item(p))

    if not items:
        print('Error: No valid paper data.')
        sys.exit(1)

    # Step 1: Save metadata
    uri = f'https://pubmed.ncbi.nlm.nih.gov/{papers[0].get("pmid", "")}/' if papers[0].get('pmid') else ''
    status, msg, session_id = save_items(items, uri)
    if status != 201:
        print(f'Failed: {msg}')
        sys.exit(1)

    print(f'Success: {msg} ({len(items)} papers)')
    for item in items:
        print(f'  - {item.get("title", "?")}')

    # Steps 2 & 3: Download PDFs and attach to Zotero items
    pdf_ok = 0
    pdf_fail = 0
    for i, (paper, item) in enumerate(zip(papers, items)):
        pdf_urls = resolve_pdf_urls(paper)
        if not pdf_urls:
            continue

        item_id = item.get('id', f'pm_{session_id}_{i}')

        pdf_bytes = None
        pdf_url = ''
        errors = []
        for candidate_url in pdf_urls:
            pdf_bytes, err = download_pdf(candidate_url)
            if pdf_bytes:
                pdf_url = candidate_url
                break
            errors.append(f'{err} ({candidate_url[:80]})')
        if not pdf_bytes or not pdf_url:
            print(f'  PDF skip: {"; ".join(errors[:3])}')
            pdf_fail += 1
            continue

        att_status, att_msg = save_attachment(session_id, item_id, pdf_bytes, pdf_url)
        if att_status in (200, 201):
            size_mb = len(pdf_bytes) / 1024 / 1024
            print(f'  PDF attached ({size_mb:.1f} MB): {item.get("title", "?")[:60]}')
            pdf_ok += 1
        else:
            print(f'  PDF attach failed ({att_status}): {att_msg[:100]}')
            pdf_fail += 1

    if pdf_ok > 0 or pdf_fail > 0:
        print(f'PDFs: {pdf_ok} attached, {pdf_fail} failed')


if __name__ == '__main__':
    main()
