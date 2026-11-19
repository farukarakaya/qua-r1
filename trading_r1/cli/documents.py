# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""Import PDFs into the local OCR transcript store."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from trading_r1.documents import DocumentError, add_pdf
from trading_r1.providers.builtins import PDF_OCR_PROFILE
from trading_r1.state.paths import StateDirNotPrivateError


def _pdfs(paths: list[Path], on_error: Callable[[str], None]) -> Iterator[Path]:
    for path in paths:
        path = path.expanduser()
        if not path.is_dir():
            yield path
            continue
        found = False
        for root, dirs, files in path.walk(on_error=lambda err: on_error(str(err))):
            dirs.sort()
            for name in sorted(files):
                pdf = root / name
                if pdf.suffix.lower() == ".pdf" and not pdf.is_symlink():
                    found = True
                    yield pdf
        if not found:
            on_error(f"{path}: no PDFs found")


def _add(args: argparse.Namespace) -> int:
    failed = False

    def report_error(message: str) -> None:
        nonlocal failed
        print(message, file=sys.stderr)
        failed = True

    paths = _pdfs(args.paths, report_error)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        pending = {}
        while True:
            while len(pending) < args.workers and (path := next(paths, None)) is not None:
                task = pool.submit(
                    add_pdf,
                    path,
                    model=args.model,
                    backfill=args.backfill_summaries,
                )
                pending[task] = path
            if not pending:
                break
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for task in finished:
                path = pending.pop(task)
                try:
                    imported = task.result()
                except (DocumentError, OSError, StateDirNotPrivateError) as err:
                    report_error(f"{path}: {err}")
                    continue
                status = "already imported" if imported.reused else "saved OCR"
                if imported.backfilled:
                    status = "added summary"
                print(f"{path}: {status} → {imported.path}", flush=True)
    return int(failed)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="trading-r1 docs", description="Import PDFs and store page-by-page OCR transcripts."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    add = sub.add_parser(
        "add",
        help="send each new PDF in full to the OCR model",
        description=f"Send each new PDF in full using the {PDF_OCR_PROFILE} profile "
        "and save its transcript locally. "
        "Identical files reuse the existing transcript without an upload.",
    )
    add.add_argument(
        "paths",
        metavar="PATH",
        type=Path,
        nargs="+",
        help="PDF or directory to scan recursively",
    )
    add.add_argument("--model", help=f"override the model in the {PDF_OCR_PROFILE} profile")
    add.add_argument("--workers", type=int, default=4, help="concurrent PDFs (default: 4)")
    add.add_argument(
        "--backfill-summaries",
        action="store_true",
        help="summarize older transcripts from saved text without uploading the PDF again",
    )
    add.set_defaults(run=_add)
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args.run(args)
