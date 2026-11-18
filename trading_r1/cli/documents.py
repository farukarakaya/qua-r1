# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""Import PDFs into the local OCR transcript store."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from trading_r1.documents import DEFAULT_OCR_MODEL, DocumentError, add_pdf
from trading_r1.state.paths import StateDirNotPrivateError


def _add(args: argparse.Namespace) -> int:
    failed = False
    for path in args.pdf:
        try:
            imported = add_pdf(path, model=args.model)
        except (DocumentError, OSError, StateDirNotPrivateError) as err:
            print(f"{path}: {err}", file=sys.stderr)
            failed = True
            continue
        status = "already imported" if imported.reused else "saved OCR"
        print(f"{path}: {status} → {imported.path}")
    return int(failed)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="trading-r1 docs", description="Import PDFs and store page-by-page OCR transcripts."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser(
        "add",
        help="send each new PDF in full to OpenAI for OCR",
        description="Send each new PDF in full to OpenAI and save its transcript locally. "
        "Identical files reuse the existing transcript without an upload.",
    )
    add.add_argument("pdf", metavar="PDF", type=Path, nargs="+", help="local PDF path(s)")
    add.add_argument(
        "--model", default=DEFAULT_OCR_MODEL, help="OpenAI model (default: %(default)s)"
    )
    add.set_defaults(run=_add)
    args = parser.parse_args(argv)
    return args.run(args)
