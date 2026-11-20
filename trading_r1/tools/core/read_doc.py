# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""Read citable pages from the local transcript store."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from trading_r1.contract.spec import Output, Provenance, Result, ToolError, ToolSpec
from trading_r1.contract.units import Code, Count, Text
from trading_r1.runtime.sources import cite_source
from trading_r1.state.documents import DOCUMENT_ID_PATTERN, TranscriptError, load_transcript

_MAX_READ_CHARS = 16_000


class ReadDocumentRequest(BaseModel):
    document_id: str = Field(pattern=DOCUMENT_ID_PATTERN, description="ID from search_docs.")
    start_page: int = Field(1, ge=1, description="First physical PDF page, inclusive.")
    end_page: int | None = Field(
        None, ge=1, description="Last page, inclusive; omit to read to the end."
    )


class ReadPage(Output):
    page: Count
    citation_index: Count = None
    markdown: Text


class ReadDocumentOutput(Output):
    source_name: Code
    page_count: Count
    pages: list[ReadPage]


async def _read(req: ReadDocumentRequest) -> Result:
    try:
        doc = await asyncio.to_thread(load_transcript, req.document_id)
    except TranscriptError as err:
        raise ToolError(str(err)) from None
    if doc is None:
        raise ToolError("no saved transcript for this document ID")

    end = req.end_page or len(doc.pages)
    if not req.start_page <= end <= len(doc.pages):
        raise ToolError(f"select a page range within pages 1 to {len(doc.pages)}")
    selected = doc.pages[req.start_page - 1 : end]
    if sum(len(page.markdown) + 64 for page in selected) > _MAX_READ_CHARS:
        raise ToolError("the selected text is too long for one read; choose a smaller page range")

    name = " ".join(doc.source_name.split())
    pages = []
    for page in selected:
        title = f"{name}, p. {page.page}"
        index = cite_source(f"urn:trading-r1:pdf:{doc.sha256}#page={page.page}", title, title)
        pages.append(ReadPage(page=page.page, citation_index=index, markdown=page.markdown))
    return Result(
        ReadDocumentOutput(source_name=name, page_count=len(doc.pages), pages=pages),
        Provenance(source="Imported PDF transcript", subject=name),
    )


SPEC = ToolSpec(
    name="read_doc",
    display_name="core.read_doc",
    result_kind="data",
    tier="core",
    description=(
        "Read saved OCR pages from an imported PDF found with search_docs. "
        "For one page set start_page and end_page to that number. "
        "Omit the range only for a short whole report. "
        "Cite each claim with the returned page's [citation_index]."
    ),
    request_schema=ReadDocumentRequest,
    handler=_read,
    output=ReadDocumentOutput,
    tags=("research", "documents", "citations"),
)
