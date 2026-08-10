"""
Extracts NASA/TM-2018-220034 Appendix A into a structured CSV seed table.

Source: Jacklin, S.A., "Small-Satellite Mission Failure Rates", NASA/TM-2018-220034,
https://ntrs.nasa.gov/citations/20190002705  (US Government work, public release)

Produces research/analysis/jacklin_appendix_a.csv with one row per failed or partially
failed small-satellite mission, 2000-2016.

HONEST LIMITATIONS -- these matter and are carried into the workbook:
  - Appendix A is a two-column table ("Partial Mission Failure" / "Total Mission
    Failure"). PDF text extraction FLATTENS those columns, so which column a given
    description sat in is NOT reliably recoverable. This script therefore does NOT
    guess: failure_severity is emitted as "Unknown (column lost in extraction)" unless
    the description text itself makes it explicit. Inferring it would be fabrication.
  - Mission name and organisation run together in the source layout; the split here is
    heuristic and may be imperfect for some rows.
  - Some Jacklin entries record failure only because "no papers or articles were written
    post launch". That is weak evidence and is flagged in its own column rather than
    being treated as an established failure.

Run: python research/analysis/extract_jacklin_entries.py
"""

import csv
import io
import re
import sys
import urllib.request
from pathlib import Path

PDF_URL = "https://ntrs.nasa.gov/api/citations/20190002705/downloads/20190002705.pdf"
HERE = Path(__file__).parent
CACHE = HERE / "_cache_jacklin.pdf"
OUT_CSV = HERE / "jacklin_appendix_a.csv"

ENTRY_BOUNDARY = r"\n(?=20[0-1][0-9]\s+(?:[\d.]+\s*kg|[\d.]+U|\dU))"

# Page furniture that PDF extraction interleaves into the middle of table cells: a
# running page number immediately followed by the repeated table header. Both must be
# stripped, and the page number must be matched with optional trailing whitespace --
# it sits at the end of a text line, not alone on one, so an anchored ^\d+$ misses it
# and leaves artefacts like "lost 6 months 30 University of Colorado".
HEADER_NOISE = re.compile(
    r"\s*\d{1,3}\s*\n\s*Year\s+Mass\s+Small Satellite\s+Partial Mission Failure\s+Total Mission Failure"
    r"|Year\s+Mass\s+Small Satellite\s+Partial Mission Failure\s+Total Mission Failure"
    r"|\n\s*\d{1,3}\s*\n",
    re.I,
)


def fetch_pdf() -> bytes:
    if CACHE.exists():
        return CACHE.read_bytes()
    req = urllib.request.Request(PDF_URL, headers={"User-Agent": "Mozilla/5.0 (research)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    CACHE.write_bytes(data)
    return data


def appendix_text(pdf: bytes) -> str:
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(pdf))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    a = text.find("Appendix A: Small Satellite Missions That Partially or Totally Failed", 20000)
    b = text.find("Appendix B: Successful Small Satellite Missions", a + 100)
    return text[a:b]


def parse_entry(raw: str):
    clean = HEADER_NOISE.sub(" ", raw)
    clean = re.sub(r"\s+", " ", clean).strip()
    m = re.match(r"^(20[0-1][0-9])\s+([\d.]+\s*kg|[\d.]+U|\dU)\s+(.*)$", clean)
    if not m:
        return None
    year, mass, rest = m.group(1), m.group(2).strip(), m.group(3).strip()

    # Name/org vs description: the description reliably begins at the first sentence-like
    # clause describing an outcome. Use the first occurrence of a known lead-in verb.
    desc_start = re.search(
        r"\b(Satellite|Successfully|Launched|Failure|Failed|The |No |After|Contact|"
        r"Communication|Presumed|Never|Two |One |Six|\d+ (months|days|weeks|hours))\b",
        rest,
    )
    if desc_start and desc_start.start() > 3:
        name_org = rest[: desc_start.start()].strip(" ,.")
        description = rest[desc_start.start():].strip()
    else:
        name_org = rest[:60].strip(" ,.")
        description = rest

    # Mission name is typically the first segment before the organisation.
    name_parts = re.split(r"\s{2,}|,\s", name_org, maxsplit=1)
    mission = name_parts[0].strip()
    org = name_parts[1].strip() if len(name_parts) > 1 else "Unknown"

    weak_evidence = bool(re.search(r"no papers or articles were written|presumed mission failure",
                                   description, re.I))
    never_heard = bool(re.search(
        r"no signals? (were |was )?receiv|no contact|never (estab|receiv|heard)|"
        r"not heard from|no communication|unable to (estab|contact)", description, re.I))

    # Only take severity when the text is explicit; otherwise refuse to guess.
    if re.search(r"partial", description, re.I):
        severity = "Partial (stated in text)"
    else:
        severity = "Unknown (table column lost in PDF extraction)"

    return {
        "year": year,
        "mass": mass,
        "mission": mission,
        "organisation": org,
        "description_verbatim": description,
        "failure_severity": severity,
        "never_heard_from": "Yes" if never_heard else "No",
        "failure_inferred_only_from_absence_of_publications": "Yes" if weak_evidence else "No",
        "source": PDF_URL,
        "source_type": "Primary (NASA Technical Memorandum)",
    }


def main():
    try:
        pdf = fetch_pdf()
    except Exception as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1

    raw_entries = [p for p in re.split(ENTRY_BOUNDARY, appendix_text(pdf))
                   if re.match(r"20[0-1][0-9]", p.strip())]
    rows = [r for r in (parse_entry(e) for e in raw_entries) if r]

    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"parsed {len(raw_entries)} raw entries -> {len(rows)} structured rows")
    print(f"never heard from:                      {sum(1 for r in rows if r['never_heard_from'] == 'Yes')}")
    print(f"failure inferred only from no-papers:  "
          f"{sum(1 for r in rows if r['failure_inferred_only_from_absence_of_publications'] == 'Yes')}")
    print(f"wrote {OUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
