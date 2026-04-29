---
name: gs-export
description: Export Google Scholar paper(s) to Zotero via BibTeX. Gets citation data from Google Scholar's cite dialog, then pushes to Zotero desktop. Supports single or batch export.
argument-hint: "[data-cid or space-separated data-cids]"
---

# Google Scholar Export to Zotero

Export Google Scholar paper citation data via BibTeX extraction and push to Zotero desktop.

## Arguments

$ARGUMENTS contains one or more data-cids (space-separated), e.g.:
- `TFS2GgoGiNUJ` — single paper
- `TFS2GgoGiNUJ abc123XYZ def456UVW` — batch export

## Steps

### Step 1: Get citation data for each paper

For each data-cid, first try the BibTeX path. If Google returns a 403 for the BibTeX endpoint, fall back to metadata from the current Google Scholar result plus the citation dialog text.

#### 1a. Fetch cite dialog to get BibTeX link (evaluate_script)

```javascript
async () => {
  const cid = "DATA_CID_HERE";
  const resp = await fetch(
    `https://scholar.google.com/scholar?q=info:${cid}:scholar.google.com/&output=cite`,
    { credentials: 'include' }
  );
  const html = await resp.text();
  const doc = new DOMParser().parseFromString(html, 'text/html');

  // Extract export links
  const links = Array.from(doc.querySelectorAll('#gs_citi a')).map(a => ({
    format: a.textContent.trim(),
    url: a.href
  }));

  // Extract citation format texts
  const citations = Array.from(doc.querySelectorAll('#gs_citt tr')).map(tr => {
    const cells = tr.querySelectorAll('td');
    return {
      style: cells[0]?.textContent?.trim() || '',
      text: cells[1]?.textContent?.trim() || ''
    };
  });

  const bibtexLink = links.find(l => l.format === 'BibTeX');
  return { cid, bibtexLink: bibtexLink?.url || '', links, citations };
}
```

#### 1b. Try the BibTeX URL (navigate_page)

Use `mcp__chrome-devtools__navigate_page`:
- url: the `bibtexLink` URL from step 1a (on `scholar.googleusercontent.com`)

This often bypasses CORS restrictions that block fetch() to googleusercontent.com, but Google may still return HTTP 403 for `scholar.bib`.

#### 1c. Read BibTeX content and detect 403 (evaluate_script)

```javascript
async () => {
  const text = document.body.innerText || document.body.textContent || '';
  if (/^403\.\s+That.?s an error\./i.test(text) || document.title.includes('403')) {
    return { error: 'bibtex_403', bibtex: '', text };
  }
  return { bibtex: text };
}
```

#### 1d. Fallback when BibTeX is blocked

If step 1c returns `error: 'bibtex_403'`, navigate back to the Google Scholar result page and build paper JSON from the result item. This keeps Zotero export usable when Google's BibTeX endpoint is blocked.

```javascript
async () => {
  const cid = "DATA_CID_HERE";
  const item = document.querySelector(`.gs_r.gs_or.gs_scl[data-cid="${cid}"]`);
  if (!item) return { error: 'not_found', message: 'Paper not found on current page. Search the title again, then retry export.' };

  const titleEl = item.querySelector('.gs_rt a');
  const title = titleEl?.textContent?.trim() || item.querySelector('.gs_rt')?.textContent?.trim() || '';
  const meta = item.querySelector('.gs_a')?.textContent?.replace(/\s+/g, ' ').trim() || '';
  const parts = meta.split(' - ');
  const authorText = parts[0]?.replace(/…/g, '').trim() || '';
  const sourceText = parts[1]?.trim() || '';
  const publisher = parts[2]?.trim() || '';
  const year = (sourceText.match(/\b(19|20)\d{2}\b/) || meta.match(/\b(19|20)\d{2}\b/) || [''])[0];
  const fullTextUrl = (item.querySelector('.gs_ggs a') || item.querySelector('.gs_or_ggsm a'))?.href || '';
  const paperUrl = titleEl?.href || '';
  const doi = (paperUrl.match(/10\.\d{4,9}\/[-._;()/:A-Z0-9]+/i) || [''])[0];

  const authors = authorText
    .split(/\s*,\s*/)
    .map(name => name.trim())
    .filter(Boolean)
    .map(name => {
      const pieces = name.split(/\s+/);
      if (pieces.length === 1) return { name };
      return { lastName: pieces.pop(), firstName: pieces.join(' ') };
    });

  return {
    dataCid: cid,
    paper: {
      pmid: "",
      dataCid: cid,
      title,
      authors,
      journal: sourceText.replace(/,\s*(19|20)\d{2}.*/, '').trim(),
      journalAbbr: "",
      pubdate: year,
      volume: "",
      issue: "",
      pages: "",
      doi,
      url: paperUrl,
      pdfUrl: fullTextUrl,
      abstract: item.querySelector('.gs_rs')?.textContent?.trim() || "",
      keywords: [],
      language: "en",
      libraryCatalog: "Google Scholar",
      pubtype: ["Journal Article"],
      publisher
    }
  };
}
```

### Step 2: Parse BibTeX and push to Zotero

Save the parsed BibTeX data, or the fallback `paper` object from step 1d, as JSON. Then call the push script resolved relative to this skill directory:

```bash
python3 /Users/burgerhuang/.codex/skills/gs-export/scripts/push_to_zotero.py /tmp/gs_papers.json
```

Before calling the script, construct a JSON file at `/tmp/gs_papers.json` containing paper data parsed from BibTeX. Parse the BibTeX yourself and create the JSON array:

```json
[
  {
    "pmid": "",
    "title": "The title from BibTeX",
    "authors": [
      {"lastName": "Smith", "firstName": "John"}
    ],
    "journal": "Journal Name",
    "journalAbbr": "",
    "pubdate": "2022",
    "volume": "14",
    "issue": "4",
    "pages": "1054",
    "doi": "",
    "pdfUrl": "https://example.com/paper.pdf",
    "abstract": "",
    "keywords": [],
    "language": "en",
    "pubtype": ["Journal Article"]
  }
]
```

**IMPORTANT**: Set `pdfUrl` from the search result's `fullTextUrl` field (the PDF link extracted by gs-search). The Python script will download the PDF and upload it to Zotero via `/connector/saveAttachment` (Zotero 7.x ignores attachments in saveItems). The script now reuses `scripts/gs_lit_utils.py` for DOI normalization, OA URL collection, HTTP retries, PDF validation, and login/subscription/captcha detection. PDF download may fail for some publishers (403, JS-redirect); these are reported as "PDF skip" with the classified failure reason.

Optional OA enrichment for attachment lookup:

```bash
GS_UNPAYWALL_EMAIL=researcher@example.edu \
python3 /Users/burgerhuang/.codex/skills/gs-export/scripts/push_to_zotero.py /tmp/gs_papers.json
```

When `GS_UNPAYWALL_EMAIL` or `UNPAYWALL_EMAIL` is set, the push script tries Unpaywall for DOI-level OA PDF links before attaching PDFs.

BibTeX fields mapping when BibTeX is available:
- `@article{key,` → `itemType: journalArticle`
- `@inproceedings{key,` → `itemType: conferencePaper`
- `@book{key,` → `itemType: book`
- `title={...}` → `title`
- `author={Last1, First1 and Last2, First2}` → `authors` array
- `journal={...}` → `journal`
- `year={...}` → `pubdate`
- `volume={...}` → `volume`
- `number={...}` → `issue`
- `pages={...}` → `pages`
- `publisher={...}` → (included in extra or publisher field)

### Step 3: Report

Single paper:
```
Exported to Zotero from Google Scholar:
  Title: {title}
  Authors: {authors}
  Journal: {journal} ({year})
  Data-CID: {dataCid}
```

Batch:
```
Exported {count} papers to Zotero from Google Scholar:
  1. {title1} ({journal1}, {year1})
  2. {title2} ({journal2}, {year2})
  ...
```

## Batch Export Optimization

For multiple papers, process sequentially to avoid CAPTCHA:
1. Get all BibTeX links in one evaluate_script call (fetch all cite dialogs)
2. Navigate to each BibTeX URL one at a time
3. For each 403 response, fall back to current-page metadata extraction for that data-cid
4. Push all to Zotero in a single batch

## CSV Table Export

To export Google Scholar result JSON or JSONL into Chinese/English candidate tables:

```bash
python3 /Users/burgerhuang/.codex/skills/gs-export/scripts/export_table.py \
  --input /tmp/gs_results.jsonl \
  --out-dir /tmp/gs_export
```

This writes:
- `候选文献总表.csv`
- `missing_fulltext.csv`

The exporter maps Google Scholar fields (`title`, `authors`, `journalYear`, `citedBy`, `href`, `fullTextUrl`, `snippet`) into the candidate table schema and marks records with a full-text URL as `has_fulltext_link`.

## Notes

- Single paper export uses 3-4 tool calls: `evaluate_script` (cite dialog) + `navigate_page` (BibTeX URL) + `evaluate_script` (read BibTeX) + `bash python` (Zotero push)
- Batch export: 2N+1 tool calls (N papers: N navigate + N evaluate + 1 bash)
- BibTeX links are on `scholar.googleusercontent.com` — CORS blocks fetch(), so we use navigate_page first. If Google returns 403, use the metadata fallback instead of stopping.
- Reuses `push_to_zotero.py` for Zotero Connector API communication
- Google Scholar BibTeX does NOT include abstract or DOI. The metadata fallback can sometimes recover DOI from publisher URLs that contain a `10.xxxx/...` path.
- Shared helper logic lives in `scripts/gs_lit_utils.py`, adapted from `literature-harvester`.
- After export, navigate back to Google Scholar page: `navigate_page` with type `back`
