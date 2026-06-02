#!/usr/bin/env python3
"""Dump all tables from a PDF for analysis."""

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
                tables = page.extract_tables() or []
                out.write(f"===== Page {i}/{len(pdf.pages)} ({len(tables)} tables) =====\n")
                for t_idx, table in enumerate(tables, start=1):
                    out.write(f"--- Table {t_idx} ---\n")
                    for row in table:
                        out.write("\t".join("" if c is None else str(c) for c in row))
                        out.write("\n")
                    out.write("\n")
                out.write("\n")
    finally:
        if args.output:
            out.close()


if __name__ == "__main__":
    main()
