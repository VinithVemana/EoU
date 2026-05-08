#!/usr/bin/env python3
"""
Parse AVC patent list PDF → CSV rows: (Company, Country, Patent Raw, Patent Cleaned, Ambiguous).

Handles patent entry formats observed in the document:
  Parenthetical:    AL (EP 3,975,559)    → country=AL, patent=EP 3,975,559
  Direct digit:     AU 2021266245        → country=AU, patent=AU 2021266245
  Direct letter:    TW I415450           → country=TW, patent=TW I415450
  Letter+slash:     ID P000090281        → country=ID, patent=ID P000090281
  Slash start:      KH /GRRP.SG/00029   → country=KH, patent=KH /GRRP.SG/00029
  No-space slash:   NG/PT/C/2020/4909   → country=NG, patent=NG/PT/C/2020/4909
  Space in number:  AU 2006 321552       → country=AU, patent=AU 2006 321552
  Re. style:        US Re. 46,924        → country=US, patent=US Re. 46,924
  Suffix B1:        ET PT173 B1          → country=ET, patent=ET PT173 B1
  Expiry note:      US 7,400,681 - Exp. Feb 11, 2026 → strips expiry

Column-major extraction key insight:
  The PDF uses a 3-column layout. Company headers can appear in any column (col-1, col-2,
  or col-3). Within each page we process col-1 first, then col-2, then col-3; within a
  column entries are sorted by y (reading order). This guarantees that a company header
  always precedes the patents below it in the same column.

  State machine uses a pending-patents list: non-left-column patents accumulated while a
  company name is being collected are held in pending and flushed when the name is
  finalised (either by a left-column patent or by a new company header appearing after
  at least one pending patent has been collected).

Usage:
    /Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python parse_avc_pdf.py AVC_data.pdf
    /Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python parse_avc_pdf.py AVC_data.pdf --output avc_patents.csv
    /Users/vinith_macbook_pro/Desktop/python3/venv314/bin/python parse_avc_pdf.py https://example.com/AVC_data.pdf --use-firecrawl
"""

import re
import csv
import sys
import os
import argparse
from collections import defaultdict


# ── column thresholds ───────────────────────────────────────────────────────────
# Standard letter-page PDF (~612 pt wide); col-1 ≈ x0 50–190, col-2 ≈ 205–390, col-3 ≈ 405–560.
COL1_MAX = 200  # x0 < 200  → col-1; x0 ≥ 200 → col-2/3. Company headers appear in any col.
COL2_MAX = 400  # x0 < 400  → col-2; x0 ≥ 400 → col-3


# ── regex patterns ──────────────────────────────────────────────────────────────

# Parenthetical: AL (EP 3,975,559) | AM (EA 039463) | AT (EP 4,009,620 C0)
PAREN_RE = re.compile(
    r'^([A-Z]{2,3})\s+\(([A-Z]{1,4}\s+[\d,./A-Za-z-]+(?:\s+C\d)?)\)\s*$'
)

# Direct: country + space + number starting with digit / uppercase / slash.
# Allows internal spaces so "AU 2006 321552", "ET PT173 B1", "KH /GP/00082 SG" match.
# Blocks '(' so parenthetical entries don't accidentally match.
DIRECT_RE = re.compile(
    r'^([A-Z]{2,3})\s+((?:Re\.\s+)?[\dA-Z/][^()]*?)\s*$'
)

# No-space slash: NG/PT/C/2020/4909 (country code runs directly into /)
NOSPACE_RE = re.compile(r'^([A-Z]{2,3})/(.*)')

# Header / footer lines to discard
SKIP_RES = [
    re.compile(r'^February\s+\d+,\s+\d{4}$', re.I),
    re.compile(r'^AVC\s+Attachment\s+\d+$', re.I),
    re.compile(r'^Page\s+\d+\s+of\s+\d+$', re.I),
    re.compile(r'^V/A\s*$', re.I),
    re.compile(r'^LICENSING$', re.I),
    re.compile(r'^ALLIANCE$', re.I),
    re.compile(r'^\d{4}$'),           # stray year from split expiry line "- Exp. Feb 11, 2026"
    re.compile(r'^-\s+Exp\.', re.I),  # residual expiry fragment
    re.compile(r'^\s*$'),
]


def should_skip(line: str) -> bool:
    s = line.strip()
    return not s or any(p.match(s) for p in SKIP_RES)


def clean_patent(raw: str) -> str:
    """Remove spaces and commas: 'EP 3,975,559' → 'EP3975559'."""
    return re.sub(r'[\s,]', '', raw)


def extract_patent(line: str) -> tuple[str, str] | None:
    """
    Return (country, patent_raw) if line is a patent entry, else None.

    Try in order: parenthetical → no-space-slash → direct.
    Expiry annotations must be stripped before calling (see parse_entries).
    """
    # 1. Parenthetical: AL (EP 3,975,559)
    m = PAREN_RE.match(line)
    if m:
        return m.group(1), m.group(2).strip()

    # 2. No-space slash: NG/PT/C/2020/4909
    m = NOSPACE_RE.match(line)
    if m:
        country = m.group(1)
        return country, line  # full original line is the raw patent

    # 3. Direct: AU 2021266245 | BR PI-0408570-1 | KH /GRRP.SG/00029 | ET PT173 B1
    m = DIRECT_RE.match(line)
    if m:
        country = m.group(1)
        number = m.group(2).strip()
        # Require at least one digit — prevents company name fragments like
        # "NTT DOCOMO, INC." (country=NTT, number="DOCOMO, INC.") from matching.
        if not re.search(r'\d', number):
            return None
        return country, f"{country} {number}"

    return None


# ── text extraction ─────────────────────────────────────────────────────────────

def extract_with_pdfplumber(pdf_path: str) -> list[dict]:
    """
    Return list of {'text': str, 'x0': float} dicts in column-major order.

    For each page, words are separated into three column buckets by x0, then each
    bucket is sorted by y (reading order within the column). Columns are emitted in
    order: col-1, col-2, col-3. This ensures company headers in col-1 are always
    processed before the col-2/col-3 patents that belong to the same section, even
    when they share the same y-rows in the physical page.
    """
    import pdfplumber

    entries: list[dict] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            if not words:
                continue

            # Separate into column buckets; bucket words by y (4 pt tolerance)
            col_rows: list[dict[int, list]] = [defaultdict(list) for _ in range(3)]

            for w in words:
                y_bucket = round(w['top'] / 4) * 4
                if w['x0'] < COL1_MAX:
                    col_rows[0][y_bucket].append(w)
                elif w['x0'] < COL2_MAX:
                    col_rows[1][y_bucket].append(w)
                else:
                    col_rows[2][y_bucket].append(w)

            # Emit col-1, then col-2, then col-3 (each in y-order)
            for col in col_rows:
                for y in sorted(col.keys()):
                    row_words = sorted(col[y], key=lambda w: w['x0'])
                    text = ' '.join(w['text'] for w in row_words).strip()
                    if text:
                        entries.append({'text': text, 'x0': row_words[0]['x0']})

    return entries


def extract_with_firecrawl(url: str) -> list[dict]:
    """
    Extract text from a PDF at an HTTP/HTTPS URL via Firecrawl.
    Returns entries with x0=0 (no column info — use for web-hosted PDFs).
    Requires FIRECRAWL_API_KEY env var.
    """
    from firecrawl import FirecrawlApp

    api_key = os.environ.get("FIRECRAWL_API_KEY")
    if not api_key:
        raise ValueError("FIRECRAWL_API_KEY not set")

    app = FirecrawlApp(api_key=api_key)
    result = app.scrape_url(url, params={"formats": ["markdown"]})
    markdown = result.get("markdown", "")
    return [{'text': line.strip(), 'x0': 0} for line in markdown.split('\n')]


# ── parsing ─────────────────────────────────────────────────────────────────────

def parse_entries(entries: list[dict]) -> list[tuple[str, str, str, str, bool]]:
    """
    Walk entries in column-major order and yield
    (company, country, patent_raw, patent_clean, ambiguous).

    State machine:
      collecting=True   — accumulating company name lines into company_buffer
      collecting=False  — company name is locked in current_company

    Company headers may appear in any column (col-1, col-2, or col-3).
    Non-left-column patents seen while collecting=True go to pending_patents and
    are flushed when:
      (a) a left-column patent triggers flush_company(), OR
      (b) a new company header line appears after ≥1 pending patent has been seen
          (signals the previous company is complete).

    Ambiguous=True is set on rows where the line looked patent-like (has digits)
    but did not match any known pattern — flagged rather than silently dropped.
    """
    rows: list[tuple[str, str, str, str, bool]] = []
    current_company: str = ''
    company_buffer: list[str] = []
    pending_patents: list[tuple[str, str]] = []
    collecting: bool = True

    def flush_company() -> None:
        nonlocal current_company, company_buffer, pending_patents, collecting
        if company_buffer:
            current_company = ' '.join(company_buffer)
            company_buffer.clear()
        for country, patent_raw in pending_patents:
            rows.append((current_company, country, patent_raw, clean_patent(patent_raw), False))
        pending_patents.clear()
        collecting = False

    for entry in entries:
        line = entry['text'].strip()
        x0 = entry.get('x0', 0)
        is_left = x0 < COL1_MAX

        if should_skip(line):
            continue

        # Strip trailing expiry: "US 7,400,681 - Exp. Feb 11, 2026"
        line = re.sub(r'\s+-\s+Exp\..*$', '', line).strip()
        if not line or should_skip(line):
            continue

        patent = extract_patent(line)

        if patent:
            country, patent_raw = patent
            if is_left:
                flush_company()
                rows.append((current_company, country, patent_raw, clean_patent(patent_raw), False))
            else:
                if collecting:
                    pending_patents.append((country, patent_raw))
                else:
                    rows.append((current_company, country, patent_raw, clean_patent(patent_raw), False))
        else:
            # Non-patent text — company name fragment (any column allowed).
            # If we already have pending patents for the current company, a new header
            # means that company is complete: flush it before starting the next one.
            if pending_patents:
                flush_company()
            if not collecting:
                collecting = True
            # Flag as ambiguous when the line looks like a patent (starts with a
            # 2–3 uppercase-letter country-code prefix) AND contains ≥3 digits in
            # the part after the prefix — i.e. it could be a patent number we
            # failed to parse.  Company name fragments like "NTT DOCOMO, INC." or
            # "LG Electronics Inc." have 0 digits after the prefix and are treated
            # as company name lines instead.
            prefix_m = re.match(r'^([A-Z]{2,3})\s+(.*)', line)
            if prefix_m and len(re.findall(r'\d', prefix_m.group(2))) >= 3:
                rows.append((current_company, '?', line, line, True))
            else:
                company_buffer.append(line)

    flush_company()  # assign any remaining pending at end of document
    return rows


# ── main ────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description='Parse AVC patent list PDF → CSV')
    parser.add_argument(
        'input',
        help='Local PDF path, or HTTP/HTTPS URL with --use-firecrawl'
    )
    parser.add_argument('--output', '-o', help='Output CSV path (default: stdout)')
    parser.add_argument(
        '--use-firecrawl', action='store_true',
        help='Use Firecrawl for extraction (requires FIRECRAWL_API_KEY; input must be HTTP/HTTPS URL)'
    )
    args = parser.parse_args()

    print(f"Extracting: {args.input}", file=sys.stderr)
    if args.use_firecrawl:
        entries = extract_with_firecrawl(args.input)
    else:
        entries = extract_with_pdfplumber(args.input)
    print(f"  {len(entries)} raw entries", file=sys.stderr)

    rows = parse_entries(entries)
    print(f"  {len(rows)} patent rows parsed", file=sys.stderr)

    header = ['Company', 'Country', 'Patent (Raw)', 'Patent (Cleaned)', 'Ambiguous']
    if args.output:
        with open(args.output, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        w = csv.writer(sys.stdout)
        w.writerow(header)
        w.writerows(rows)


if __name__ == '__main__':
    main()
