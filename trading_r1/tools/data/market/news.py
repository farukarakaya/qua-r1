# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""data.market.news: the latest market news, one security's news, or a search by topic.

Explicit BIST symbols get recent company disclosures from KAP via borsapy.

Headline, publisher, time, link and Tauric's summary for each article, with its sentiment,
impact and event types, so the model sees what is being reported and can `visit` a link for the
article. A query ranks articles by relevance; without one the newest come first, and Tauric
collapses syndicated copies into one story. Times are when Tauric published-or-discovered the
item: the basis is stated per item.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

from trading_r1.contract.spec import Output, Provenance, Result, ToolError, ToolSpec, hint
from trading_r1.contract.units import Code
from trading_r1.tools.data import articles, bist
from trading_r1.tools.data.articles import NEWS_PAGE, Article, article
from trading_r1.tools.data.common import Symbol
from trading_r1.tools.data.tauric import Tauric, provenance

_DESCRIPTION = (
    "News from Tauric's finance news store: the latest market news, one security's news "
    "(`symbol`), or a search by topic (`query`, e.g. 'fed rate decision', 'memory chip "
    "prices', 'Boeing 737 deliveries'; relevance-ranked; combine with `symbol` to search "
    "within one security). Without a query the newest come first. Optionally between `start` "
    "and `end` or of one `sentiment` (bullish, bearish, mixed, neutral); a search without "
    "dates spans the whole store, so give `start` for recent coverage. Each article: headline, "
    "publisher, time (UTC; `time_basis` says whether it is the article's publication time or "
    "when Tauric discovered it), link, a short summary, and Tauric's read: sentiment, impact "
    "(low/medium/high) and event types. Each article is a numbered source [N] (`source`): "
    "cite it by that number, and open it with `visit` when the summary is not enough; the web "
    "`search` covers what this store does not. For BIST (XIST:THYAO or THYAO.IS), returns "
    "recent KAP company disclosures instead; dates and optional title text filter this recent "
    "feed, and sentiment is not supported."
)
# borsapy's KAP provider constructs disclosure links in this exact form.
_KAP_LINK = re.compile(r"https://www\.kap\.org\.tr/tr/Bildirim/[0-9]+")


class NewsRequest(BaseModel):
    """Input for news."""

    symbol: Symbol | None = Field(None, description="Narrow to one security (e.g. AAPL).")
    query: (
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]
        | None
    ) = Field(None, description="What to search for; omit for the latest news.")
    start: dt.date | None = Field(None, description="Articles from this date (UTC).")
    end: dt.date | None = Field(None, description="Articles up to this date, inclusive (UTC).")
    sentiment: Literal["bullish", "bearish", "mixed", "neutral"] | None = Field(None)
    limit: int = Field(10, ge=1, le=NEWS_PAGE, description="Articles to return.")


class NewsOutput(Output):
    """The scope and its articles: newest first, or most relevant first for a query."""

    symbol: Code = None
    query: Code = None
    items: Annotated[list[Article], hint(rows=NEWS_PAGE)]


def _story(raw: dict) -> dict | None:
    """A story row: its newest article, with the number of copies when there are several."""
    item = article(raw)
    if item and (raw.get("story_members") or 1) > 1:
        item["copies"] = raw["story_members"]
    return item


def _disclosures(bp: Any, ticker: str, req: NewsRequest) -> list[dict[str, Any]]:
    zone = bist.istanbul()
    query = req.query.casefold() if req.query else None
    items = []
    seen: set[str] = set()
    for row in bp.Ticker(ticker).news.to_dict("records"):
        title, url = row.get("Title"), row.get("URL")
        if (
            not isinstance(title, str)
            or not title.strip()
            or (query and query not in title.casefold())
        ):
            continue
        if not isinstance(url, str) or url in seen or not _KAP_LINK.fullmatch(url):
            continue
        try:
            local = dt.datetime.strptime(row.get("Date"), "%d.%m.%Y %H:%M:%S")
        except (TypeError, ValueError):
            continue
        published = local.replace(tzinfo=zone).astimezone(dt.UTC)
        day = published.date()
        if (req.start and day < req.start) or (req.end and day > req.end):
            continue
        seen.add(url)
        items.append(
            {
                "title": title,
                "url": url,
                "published": published.isoformat().replace("+00:00", "Z"),
                "publisher": "KAP",
                "time_basis": "published",
            }
        )
    items.sort(key=lambda item: item["published"], reverse=True)
    return items[: req.limit]


async def _bist_news(req: NewsRequest, ticker: str) -> Result:
    if req.sentiment:
        raise ToolError("KAP company disclosures do not support a sentiment filter.")
    if req.start and req.end and req.start > req.end:
        raise ToolError("Start must be on or before end.")
    subject = f"XIST:{ticker}"
    items = await bist.run(subject, "KAP disclosures", lambda bp: _disclosures(bp, ticker, req))
    if not items:
        raise ToolError(
            f"No matching recent KAP disclosures for {subject}. The KAP feed is not a "
            "historical archive; try `search` for older disclosures."
        )
    articles.number(items)
    return Result(
        NewsOutput(symbol=subject, query=req.query, items=items),
        Provenance(
            **provenance(
                subject=subject,
                as_of=items[0]["published"],
                source="KAP (via borsapy)",
            )
        ),
    )


async def _handler(req: NewsRequest) -> Result:
    if req.symbol and (ticker := bist.bist_ticker(req.symbol)):
        return await _bist_news(req, ticker)
    window = {
        "since": req.start and f"{req.start.isoformat()}T00:00:00Z",
        "until": req.end and f"{req.end.isoformat()}T23:59:59.999999Z",
        "sentiment": req.sentiment,
        "instrument": req.symbol and f"security:{req.symbol}",
        "limit": NEWS_PAGE,
    }
    async with Tauric() as api:
        if req.query:
            body = await api.get("/news/search", q=req.query, **window)
        else:
            body = await api.get("/news", collapse="story", **window)
    if req.query:
        found = [a for d in body.get("data") or [] if (a := article(d.get("article") or {}))]
        items = articles.collapse(found)
    else:
        items = [i for i in (_story(r) for r in body.get("data") or []) if i]
        items.sort(key=lambda i: i["published"] or "", reverse=True)
    items = items[: req.limit]
    if not items:
        scope = f" for '{req.symbol}'" if req.symbol else ""
        matched = f" matched {req.query!r}" if req.query else ""
        widen = "widen the dates, " if req.start or req.end else ""
        raise ToolError(f"no news{matched}{scope}: {widen}or try `search`")
    articles.number(items)
    return Result(
        NewsOutput(symbol=req.symbol, query=req.query, items=items),
        Provenance(
            **provenance(
                subject=req.symbol or req.query or "market",
                as_of=None if req.query else items[0]["published"],
                source="Tauric news",
            )
        ),
    )


SPEC = ToolSpec(
    name="data_market_news",
    display_name="data.market.news",
    result_kind="data",
    tier="deferred",
    description=_DESCRIPTION,
    request_schema=NewsRequest,
    handler=_handler,
    output=NewsOutput,
    tags=("market", "news", "search", "headlines", "data"),
)
