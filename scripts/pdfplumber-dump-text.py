#!/usr/bin/env python3
"""Dump all pages of a PDF as text for analysis."""

import argparse
import sys

import pdfplumber


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument(
        "-o", "--output", help="Write to file instead of stdout"
    )
    args = parser.parse_args()

    out = open(args.output, "w") if args.output else sys.stdout
    try:
        with pdfplumber.open(args.pdf) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                text = page.extract_text() or ""
                out.write(f"===== Page {i}/{len(pdf.pages)} =====\n")
                out.write(text)
                out.write("\n\n")
    finally:
        if args.output:
            out.close()


if __name__ == "__main__":
    main()
