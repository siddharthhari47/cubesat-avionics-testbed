"""
Reproducible classification of NASA/TM-2018-220034 Appendix A.

Source: Jacklin, S.A., "Small-Satellite Mission Failure Rates", NASA/TM-2018-220034,
NASA Ames Research Center, March 2019.
https://ntrs.nasa.gov/citations/20190002705
(US Government work; NTRS public release.)

Appendix A of that report lists every small-satellite mission that partially or totally
failed between 2000 and 2016, with the cause identified "when known". This script
downloads the PDF, extracts Appendix A, and classifies each entry by failure category.

WHY THIS EXISTS: the central research question is what fraction of CubeSat failure is
potentially recoverable/containable with earlier detection and bounded autonomy. That
question cannot be answered honestly without first measuring how much of the failure
record is *knowable at all*. This script measures exactly that.

METHOD AND ITS LIMITS -- read before quoting any number from this:
  - This is regex/keyword classification over PDF-extracted prose. It is APPROXIMATE.
    Entry boundaries are inferred from "<year> <mass>" line starts and may occasionally
    split or merge an entry.
  - Categories deliberately OVERLAP. A mission can be counted in several (the research
    brief explicitly asks not to force single-category assignment). Percentages
    therefore do not sum to 100.
  - "No identifiable technical cause" means *this NASA document* does not state one.
    Other sources sometimes do; a handful of these missions are documented in detail
    elsewhere. It is a measure of this dataset's resolution, not of ground truth.
  - Jacklin marks some missions as failed on the basis that "no papers or articles were
    written post launch". That is weak evidence of failure and is counted separately
    here rather than being silently folded into the failure statistics.

Every number this produces is SOURCE-DERIVED INFERENCE (our analysis of a NASA source),
not FACT (something the NASA source states directly).

Run: python research/analysis/jacklin_appendix_analysis.py
"""

import re
import sys
import urllib.request
from pathlib import Path

PDF_URL = "https://ntrs.nasa.gov/api/citations/20190002705/downloads/20190002705.pdf"
CACHE = Path(__file__).parent / "_cache_jacklin.pdf"

# Entry boundary: a line starting "<year> <mass>" e.g. "2003 3U" or "2000 5.9 kg"
ENTRY_BOUNDARY = r"\n(?=20[0-1][0-9]\s+(?:[\d.]+\s*kg|[\d.]+U|\dU))"

PATTERNS = {
    "never_heard_from": (
        r"no signals? (were |was )?receiv|no contact (could be |was )?(estab|made)|"
        r"never (estab|receiv|heard|contact)|not heard from|no communication|"
        r"contact was never|unable to (estab|contact)|no beacon"
    ),
    "presumed_failed_no_publications": r"no papers or articles were written|presumed mission failure",
    "power_eps": r"power system|batter|solar (array|panel|cell)|charg|depleted|undervolt|brown ?out|eps\b",
    "communications": r"transmitter|transceiver|radio|downlink|uplink|beacon|communication (system|subsystem|fail)",
    "adcs": r"reaction wheel|attitude|gyro|magnetorq|torque (coil|rod)|tumbl|star tracker|spin (rate|up)|pointing",
    "software_obc": r"software|flight code|computer|processor|firmware|memory|reboot|watchdog|\bobc\b|corrupt",
    # deliberately NOT bare "deploy" -- "deployed into orbit" appears in nearly every
    # DOA description and inflated this category to 35% on a first pass.
    "deployment_mechanical": (
        r"(fail|did not|unable|could not).{0,40}(deploy|separat)|deploy\w* (fail|mechanism|anomal)|"
        r"did not deploy|failed to separate|stuck|jam(med)?\b|antenna.{0,20}(did not|fail)"
    ),
    "thermal": r"thermal|temperature|overheat|degrees",
    "radiation": r"radiation|single[- ]event|latch[- ]?up|\bseu\b|\bsel\b|\btid\b",
    "degraded_mode_continued": (
        r"mission to continue|continued? (to operate|on|in)|back-?up|"
        r"redundant (wheel|unit|system)|suboptimal|partial (operation|success)|work-?around"
    ),
}

TECHNICAL_CAUSE_KEYS = [
    "power_eps", "communications", "adcs", "software_obc",
    "deployment_mechanical", "thermal", "radiation",
]


def fetch_pdf() -> bytes:
    if CACHE.exists():
        return CACHE.read_bytes()
    req = urllib.request.Request(PDF_URL, headers={"User-Agent": "Mozilla/5.0 (research)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    CACHE.write_bytes(data)
    return data


def extract_appendix_a(pdf_bytes: bytes) -> str:
    import pypdf
    import io
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    start = text.find("Appendix A: Small Satellite Missions That Partially or Totally Failed", 20000)
    end = text.find("Appendix B: Successful Small Satellite Missions", start + 100)
    if start < 0 or end < 0:
        raise RuntimeError("Appendix A markers not found -- the document layout may have changed")
    return text[start:end]


def classify(appendix_text: str):
    entries = [p for p in re.split(ENTRY_BOUNDARY, appendix_text)
               if re.match(r"20[0-1][0-9]", p.strip())]
    counts = {}
    for name, pattern in PATTERNS.items():
        counts[name] = sum(1 for e in entries if re.search(pattern, e, re.I))
    no_cause = sum(
        1 for e in entries
        if not any(re.search(PATTERNS[k], e, re.I) for k in TECHNICAL_CAUSE_KEYS)
    )
    return entries, counts, no_cause


def main():
    try:
        pdf = fetch_pdf()
    except Exception as e:
        print(f"could not fetch {PDF_URL}: {e}", file=sys.stderr)
        return 1

    appendix = extract_appendix_a(pdf)
    entries, counts, no_cause = classify(appendix)
    n = len(entries)

    print(f"Source: NASA/TM-2018-220034 Appendix A ({PDF_URL})")
    print(f"Entries parsed: {n}   (Jacklin's own text reports 198 failed/partially-failed missions)")
    print()
    print(f"{'category':<44}{'n':>5}{'% of entries':>14}")
    print("-" * 63)
    for name, v in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{name:<44}{v:>5}{v / n * 100:>13.1f}%")
    print("-" * 63)
    print(f"{'NO identifiable technical cause in source':<44}{no_cause:>5}{no_cause / n * 100:>13.1f}%")
    print()
    print("Categories overlap by design; percentages do not sum to 100.")
    print("All figures are SOURCE-DERIVED INFERENCE, not statements made by the source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
