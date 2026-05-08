import argparse
import json
import os
import re
import sys
from pathlib import Path

import fitz  # PyMuPDF
from openai import OpenAI

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
DOCS_DIR = Path(os.environ.get("DOCS_DIR", "/docs"))

SCAN_PAGES = 10
TEXT_CAP = 8_000
MODEL = "gpt-4o-mini"

openai_client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """\
You are a technical documentation classifier for network vendor PDFs.
Given text from the first pages of a PDF plus its filename, extract metadata and return JSON.

Fields:
  vendor      — lowercase vendor name (e.g. "cisco", "juniper", "arista", "palo-alto", "fortinet", "nokia", "hpe")
  product     — lowercase product or OS name (e.g. "ios-xe", "junos", "eos", "pan-os", "sr-os", "aos-cx")
  version     — version string if clearly visible (e.g. "17.9.1", "23.2R1", "4.28.0") or null
  doc_type    — one of: cli-reference, config-guide, design-guide, validated-design, release-notes,
                white-paper, datasheet, or null
  trust_tier  — integer: 1 for standard vendor documentation; 2 for vendor-published validated or
                recommended designs (CVDs, Validated Solution Guides, reference architectures); null if uncertain
  source_type — one of: "vendor-doc" for standard documentation; "validated-design" for CVDs and
                validated solution guides; null if uncertain

Classification guidance for trust_tier and source_type:
- Set trust_tier=2 and source_type=validated-design for: HPE Aruba Validated Solution Guides,
  Cisco CVDs, Juniper reference architectures, and any document titled "Validated Design",
  "Reference Architecture", "Solution Guide", "CVD", or similar.
- Set trust_tier=1 and source_type=vendor-doc for standard CLI references, configuration guides,
  release notes, white papers, and datasheets.
- Set doc_type=validated-design when the document is a CVD or validated solution guide.

Return ONLY a JSON object with exactly these six keys. Use null for any field you cannot determine with confidence.\
"""


def extract_first_pages(pdf_path: Path) -> str:
    doc = fitz.open(pdf_path)
    parts = []
    for i, page in enumerate(doc):
        if i >= SCAN_PAGES:
            break
        text = page.get_text().strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)[:TEXT_CAP]


def generate_sidecar(pdf_path: Path) -> dict:
    text = extract_first_pages(pdf_path)
    if not text:
        return {}

    resp = openai_client.chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Filename: {pdf_path.name}\n\n{text}"},
        ],
    )

    raw = json.loads(resp.choices[0].message.content)
    result = {}
    for k, v in raw.items():
        if v is None:
            continue
        s = str(v).lower()
        if k == "version":
            # Strip placeholder segments like .xxxx or .x from template PDFs
            s = re.sub(r"(\.[xX]+)+$", "", s)
        result[k] = s
    return result


def main():
    parser = argparse.ArgumentParser(description="Generate draft JSON sidecars for PDFs")
    parser.add_argument(
        "files",
        nargs="*",
        help="PDFs to process (default: all PDFs under DOCS_DIR without a sidecar)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing sidecars",
    )
    args = parser.parse_args()

    if args.files:
        targets = [Path(f) for f in args.files]
    else:
        targets = sorted(DOCS_DIR.glob("**/*.pdf"))

    if not targets:
        print(f"No PDFs found in {DOCS_DIR}")
        sys.exit(0)

    done = skipped = failed = 0
    for pdf in targets:
        sidecar = pdf.with_suffix(".json")
        if sidecar.exists() and not args.force:
            print(f"  skip   {pdf.name}  (sidecar exists — use --force to overwrite)")
            skipped += 1
            continue

        print(f"  scan   {pdf.name} … ", end="", flush=True)
        try:
            meta = generate_sidecar(pdf)
            if not meta:
                print("no extractable text — skipped")
                skipped += 1
                continue
            sidecar.write_text(json.dumps(meta, indent=2) + "\n")
            print(", ".join(f"{k}={v}" for k, v in meta.items()))
            done += 1
        except Exception as exc:
            print(f"FAILED: {exc}")
            failed += 1

    print(f"\n{'─' * 60}")
    print(f"Generated {done}  |  skipped {skipped}  |  failed {failed}")
    if done:
        print("\nReview the .json files in docs/, edit any incorrect values,")
        print("then re-ingest to apply metadata:  make ingest-force")


if __name__ == "__main__":
    main()
