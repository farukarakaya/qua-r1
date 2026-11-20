# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""Search saved PDF transcripts without sending the catalog to the model."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from trading_r1.contract.spec import Output, Provenance, Result, ToolSpec
from trading_r1.contract.units import Code, Count, Text
from trading_r1.state.documents import search_transcripts

ReportPrefix = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)
]


class SearchDocsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str | None = Field(None, max_length=240, description="Keywords or a report filename.")
    tickers: list[ReportPrefix] | None = Field(
        None, max_length=20, description="Match any ticker at the start of a filename, e.g. LOGO."
    )
    scope: Literal["summary", "all"] = Field(
        "all", description="Search filenames and summaries, or also search the OCR pages (all)."
    )
    limit: int = Field(8, ge=1, le=20, description="Maximum hits to return.")
    offset: int = Field(0, ge=0, description="Skip this many ranked hits. Use next_offset.")


class SearchDocHit(Output):
    document_id: Code
    source_name: Code
    page_count: Count
    matched_page_count: Count
    matched_pages: list[int]
    summary: Text = None


class SearchDocsOutput(Output):
    total_matched: Count
    omitted: Count
    next_offset: Count = None
    hits: list[SearchDocHit]


async def _search(req: SearchDocsRequest) -> Result:
    found = await asyncio.to_thread(
        search_transcripts,
        req.query,
        req.tickers,
        scope=req.scope,
        limit=req.limit,
        offset=req.offset,
    )
    end = req.offset + len(found.hits)
    return Result(
        SearchDocsOutput(
            total_matched=found.total_matched,
            omitted=found.omitted,
            next_offset=end if end < found.total_matched else None,
            hits=[
                SearchDocHit(
                    document_id=hit.document_id,
                    source_name=hit.source_name,
                    page_count=hit.page_count,
                    matched_page_count=hit.matched_page_count,
                    matched_pages=hit.matched_pages,
                    summary=hit.summary,
                )
                for hit in found.hits
            ],
        ),
        Provenance(source="Imported PDF transcripts", subject="Report search"),
    )


SPEC = ToolSpec(
    name="search_docs",
    display_name="core.search_docs",
    tier="core",
    result_kind="data",
    description=(
        "Search the full local library of imported PDF reports by ticker, filename or keywords. "
        "Summaries are leads, not evidence. Open the document_id and relevant matched_pages "
        "(up to 12 per hit) with read_doc to verify and cite the original pages."
    ),
    request_schema=SearchDocsRequest,
    handler=_search,
    output=SearchDocsOutput,
    tags=("research", "documents", "search"),
)
