#!/usr/bin/env python3
# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0
"""Download the latest InvestingPro PDF report for every BIST ticker in a list.

The script reads `bist_stocks.txt` by default, looks up each ticker on
tr.investing.com, and downloads the report to `custom_report_feed/` using the
name `<TICKER>_<YYYY-MM-DD>.pdf`. Tickers without an instrument or without a
published report are skipped.

Authentication
--------------
One value is required. Put it in the repository `.env` (it is gitignored):

    INVESTING_COOKIE=...

`INVESTING_COOKIE` is the full `cookie:` header sent by the GraphQL request on
`https://tr.investing.com/pro/IBSE:<TICKER>`. The cookie is bound to your IP
address and browser user agent, so refresh it on the machine that runs this
script.

How to get it from Chrome:
  1. Open https://tr.investing.com/pro/IBSE:ATATP while logged in.
  2. Open DevTools -> Network, filter for `query?raw=true`.
  3. Right-click the request -> Copy -> Copy as cURL.
  4. Copy everything after `-H 'cookie: ` up to the closing quote into
     `INVESTING_COOKIE`.

The report token (`INVESTING_ACCESS_TOKEN`, a JWT valid for about an hour) is
minted from the cookie on every run, and is refreshed when it is about to
expire. Set it only to override the minted token. Both values may also be
pasted as a complete `cookie: ...` or `authorization: ...` line; the script
strips the header name.

Requires `curl_cffi`: Cloudflare rejects requests that do not carry a browser
TLS fingerprint, so the script impersonates Chrome. Run it as:

    uv run --with curl_cffi python scripts/fetch_bist_reports.py

Usage
-----
    uv run --with curl_cffi python scripts/fetch_bist_reports.py
    uv run --with curl_cffi python scripts/fetch_bist_reports.py --limit 5 --workers 2
    uv run --with curl_cffi python scripts/fetch_bist_reports.py \
        --tickers-file bist_stocks.txt --out-dir custom_report_feed

The command is safe to re-run: a report whose `<TICKER>_<date>.pdf` already
exists is not downloaded again.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

try:
    from curl_cffi import requests as cffi
except ImportError:  # checked in main() so --help still works
    cffi = None

ASSET_QUERY = """
query loadAsset ($ticker: String!, $currency: String) {
  asset: company (ticker: $ticker, currency: $currency) {
    name
    short_name
    type
    ticker
    exchange_ticker: full_ticker
    full_ticker: _full_ticker
    slug
    sector
    sector_id
    industry
    country
    country_id: isocountry2
    exchange
    analyst
    active
    enabled
    delisted
    region
    region_id
    is_subscribed
    index
    trading_status
    trading_currency
    reporting_currency
    reporting_scale
    reporting_frequency
    profile
    sources
  }
}
"""

GRAPHQL_URL = "https://tr.investing.com/pro/_/api/query?raw=true"
LOGIN_URL = "https://auth.eu.api.investing.com/investing.auth.Auth/Login"
REPORT_URL = (
    "https://endpoints.investing.com/content-summary/api/v1/"
    "pro-reports/domains/{domain}/instruments/{instrument}/pdf/desktop"
)
DOMAIN_ID = "10"  # tr.investing.com
EDITION = "tr"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_RATE = 1.0  # media.investing.com answers 429 above roughly one request per second
TOKEN_MARGIN = 120  # seconds before expiry at which the token is refreshed


class AuthError(RuntimeError):
    """Credentials were rejected; stop the run instead of retrying every ticker."""


class HttpError(RuntimeError):
    def __init__(self, status: int, what: str) -> None:
        self.status = status
        super().__init__(what if status == 0 else f"{what} returned HTTP {status}")


@dataclass(frozen=True)
class Report:
    date: str
    url: str


@dataclass(frozen=True)
class Outcome:
    ticker: str
    state: str  # downloaded, skipped, failed
    detail: str


def load_env(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE lines without requiring python-dotenv."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def setting(env: dict[str, str], name: str) -> str:
    return (os.environ.get(name) or env.get(name) or "").strip()


def clean_header(value: str, header: str, bearer: bool = False) -> str:
    value = re.sub(r"^-H\s+", "", value.strip())
    value = value.strip("\"'")
    value = re.sub(rf"^{re.escape(header)}\s*:\s*", "", value, flags=re.I)
    if bearer:
        value = re.sub(r"^bearer\s+", "", value, flags=re.I)
    return value.strip().strip("\"'")


def jwt_expiry(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError, binascii.Error):
        return None
    exp = data.get("exp")
    return int(exp) if isinstance(exp, (int, float)) else None


def report_date(url: str) -> str | None:
    """Extract the report date from the base64 name in the media URL."""
    path = urlsplit(url).path
    marker = "/pro-report/"
    if marker in path:
        token = path.split(marker, 1)[1].rstrip("/")
    else:
        token = path.rstrip("/").rsplit("/", 1)[-1]
    if not token:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode(
            "utf-8", "replace"
        )
    except (ValueError, TypeError, binascii.Error):
        return None
    match = re.search(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)", decoded)
    return match.group(1) if match else None


_thread_state = threading.local()


def _session() -> cffi.Session:
    """One Chrome-impersonating session per worker thread, so connections are reused."""
    current = getattr(_thread_state, "session", None)
    if current is None:
        current = cffi.Session(impersonate="chrome")
        _thread_state.session = current
    return current


def request_json(
    url: str,
    headers: dict[str, str],
    cookie: str | None = None,
    token: str | None = None,
    payload: dict | None = None,
    timeout: int = 45,
) -> tuple[int, bytes]:
    merged = dict(headers)
    if cookie:
        merged["cookie"] = cookie
    if token:
        merged["authorization"] = f"Bearer {token}"
    try:
        if payload is None:
            response = _session().get(url, headers=merged, timeout=timeout)
        else:
            response = _session().post(url, headers=merged, json=payload, timeout=timeout)
    except cffi.RequestsError as exc:
        raise HttpError(0, f"request failed: {exc}") from exc
    return response.status_code, response.content


def graphql_headers(ticker: str) -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": "https://tr.investing.com",
        "priority": "u=1, i",
        "referer": f"https://tr.investing.com/pro/IBSE:{ticker}",
        "referrer-policy": "no-referrer-when-downgrade",
        "sec-ch-ua": '"Not?A_Brand";v="99", "Chromium";v="130"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": USER_AGENT,
        "x-accept-language": EDITION,
        "x-domain-edition": EDITION,
        "x-original-referer": "",
    }


def report_headers() -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://tr.investing.com",
        "priority": "u=1, i",
        "referer": "https://tr.investing.com/",
        "sec-ch-ua": '"Not?A_Brand";v="99", "Chromium";v="130"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": USER_AGENT,
        "x-accept-language": EDITION,
        "x-domain-edition": EDITION,
        "x-original-referer": "",
    }


def login_headers() -> dict[str, str]:
    return {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "domain-id": EDITION,
        "origin": "https://tr.investing.com",
        "referer": "https://tr.investing.com/",
        "user-agent": USER_AGENT,
    }


def with_retries(call):
    for attempt in range(MAX_ATTEMPTS):
        try:
            status, body = call()
            if status in RETRYABLE_STATUS and attempt + 1 < MAX_ATTEMPTS:
                time.sleep(2**attempt)
                continue
            return status, body
        except HttpError:
            if attempt + 1 >= MAX_ATTEMPTS:
                raise
            time.sleep(2**attempt)
    raise AssertionError("unreachable")


def login_token(cookie: str) -> str | None:
    """Mint a report token from the session cookie."""
    status, body = with_retries(lambda: request_json(LOGIN_URL, login_headers(), cookie=cookie))
    if status in (401, 403):
        raise AuthError("token refresh was rejected (expired cookie); refresh INVESTING_COOKIE")
    if status != 200:
        raise HttpError(status, "token refresh")
    try:
        token = json.loads(body).get("Token")
    except (json.JSONDecodeError, AttributeError):
        return None
    return token if isinstance(token, str) and token else None


class TokenSource:
    """Report token shared by the workers, re-minted from the cookie before it expires."""

    def __init__(self, cookie: str, token: str = "") -> None:
        self._cookie = cookie
        self._token = token
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            expiry = jwt_expiry(self._token)
            if not self._token or (expiry is not None and expiry <= time.time() + TOKEN_MARGIN):
                token = login_token(self._cookie)
                if not token:
                    raise AuthError("could not mint an access token; refresh INVESTING_COOKIE")
                self._token = token
            return self._token


def find_instrument(ticker: str, cookie: str) -> str | None:
    status, body = with_retries(
        lambda: request_json(
            GRAPHQL_URL,
            graphql_headers(ticker),
            cookie=cookie,
            payload={
                "query": ASSET_QUERY,
                "variables": {"ticker": f"IBSE:{ticker}"},
            },
        )
    )
    if status in (401, 403):
        raise AuthError(
            "asset lookup was rejected (expired cookie or Cloudflare block); "
            "refresh INVESTING_COOKIE"
        )
    if status == 404:
        return None
    if status != 200:
        raise HttpError(status, "asset lookup")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HttpError(0, "asset lookup returned invalid JSON") from exc
    asset = (data.get("data") or {}).get("asset")
    if not isinstance(asset, dict):
        return None
    sources = asset.get("sources")
    instrument = sources.get("investing") if isinstance(sources, dict) else None
    return str(instrument) if instrument else None


def find_report(instrument: str, tokens: TokenSource) -> Report | None:
    url = REPORT_URL.format(domain=DOMAIN_ID, instrument=instrument)
    status, body = with_retries(lambda: request_json(url, report_headers(), token=tokens.get()))
    if status in (401, 403):
        raise AuthError("report lookup was rejected; refresh INVESTING_COOKIE")
    if status in (204, 404):  # no report published for this instrument
        return None
    if status != 200:
        raise HttpError(status, "report lookup")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HttpError(0, "report lookup returned invalid JSON") from exc
    report_url = data.get("url")
    if not isinstance(report_url, str) or not report_url.startswith("https://"):
        return None
    return Report(date=report_date(report_url) or date.today().isoformat(), url=report_url)


class RateLimiter:
    """Space requests across the worker threads so the media CDN does not answer 429."""

    def __init__(self, per_second: float) -> None:
        self._interval = 1 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self._interval
        if slot > now:
            time.sleep(slot - now)


_download_slots = RateLimiter(DOWNLOAD_RATE)


def download(url: str, destination: Path, destination_dir: Path) -> None:
    fd, temp_name = tempfile.mkstemp(
        dir=destination_dir, prefix=f".{destination.stem}.", suffix=".part"
    )
    os.close(fd)
    temp = Path(temp_name)
    headers = {
        "accept": "application/pdf,*/*",
        "referer": "https://tr.investing.com/",
        "user-agent": USER_AGENT,
    }
    try:
        for attempt in range(DOWNLOAD_ATTEMPTS):
            _download_slots.wait()
            try:
                response = _session().get(url, headers=headers, timeout=180)
            except cffi.RequestsError as exc:
                raise HttpError(0, f"download failed: {exc}") from exc
            if response.status_code == 429 and attempt + 1 < DOWNLOAD_ATTEMPTS:
                time.sleep(10 * 2**attempt)
                continue
            if response.status_code != 200:
                raise HttpError(response.status_code, "download")
            content = response.content
            if not content.startswith(b"%PDF"):
                raise HttpError(0, "download did not return a PDF")
            temp.write_bytes(content)
            os.replace(temp, destination)
            return
        raise HttpError(429, "download")
    finally:
        temp.unlink(missing_ok=True)


def process(
    ticker: str,
    out_dir: Path,
    cookie: str,
    tokens: TokenSource,
    auth_failed: threading.Event,
) -> Outcome:
    if auth_failed.is_set():
        return Outcome(ticker, "skipped", "authentication failed earlier in the run")
    try:
        instrument = find_instrument(ticker, cookie)
        if instrument is None:
            return Outcome(ticker, "skipped", "not on investing.com")
        report = find_report(instrument, tokens)
        if report is None:
            return Outcome(ticker, "skipped", "no report published")
        destination = out_dir / f"{ticker}_{report.date}.pdf"
        if destination.is_file() and destination.stat().st_size > 0:
            return Outcome(ticker, "skipped", f"already have {destination.name}")
        download(report.url, destination, out_dir)
        return Outcome(ticker, "downloaded", destination.name)
    except AuthError as exc:
        auth_failed.set()
        return Outcome(ticker, "failed", str(exc))
    except HttpError as exc:
        return Outcome(ticker, "failed", str(exc))
    except OSError as exc:
        return Outcome(ticker, "failed", f"local file error: {exc}")


def read_tickers(path: Path) -> list[str]:
    tickers: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        ticker = raw.strip().upper()
        if not ticker or ticker.startswith("#"):
            continue
        ticker = ticker.split()[0]
        if not re.fullmatch(r"[A-Z0-9]{2,12}", ticker):
            raise SystemExit(f"bad ticker {raw!r} in {path}")
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--tickers-file", type=Path, default=root / "bist_stocks.txt")
    parser.add_argument("--out-dir", type=Path, default=root / "custom_report_feed")
    parser.add_argument("--env-file", type=Path, default=root / ".env")
    parser.add_argument("--workers", type=int, default=4, help="parallel tickers (default: 4)")
    parser.add_argument("--limit", type=int, default=0, help="only process the first N tickers")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if cffi is None:
        print(
            "error: curl_cffi is required; run this script as "
            "`uv run --with curl_cffi python scripts/fetch_bist_reports.py`",
            file=sys.stderr,
        )
        return 2
    env = load_env(args.env_file)
    cookie = clean_header(setting(env, "INVESTING_COOKIE"), "cookie")
    if not cookie:
        print(
            f"error: missing INVESTING_COOKIE in {args.env_file} or the environment",
            file=sys.stderr,
        )
        return 2
    token = clean_header(setting(env, "INVESTING_ACCESS_TOKEN"), "authorization", bearer=True)
    tokens = TokenSource(cookie, token)
    try:
        tokens.get()
    except (AuthError, HttpError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.tickers_file.is_file():
        print(f"error: ticker list not found: {args.tickers_file}", file=sys.stderr)
        return 2
    tickers = read_tickers(args.tickers_file)
    if args.limit:
        tickers = tickers[: args.limit]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    workers = max(1, args.workers)

    print(f"downloading {len(tickers)} tickers to {args.out_dir} with {workers} worker(s)")
    auth_failed = threading.Event()
    counts = {"downloaded": 0, "skipped": 0, "failed": 0}
    width = len(str(len(tickers)))
    started = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(process, ticker, args.out_dir, cookie, tokens, auth_failed): ticker
                for ticker in tickers
            }
            for index, future in enumerate(as_completed(futures), start=1):
                outcome = future.result()
                counts[outcome.state] += 1
                print(
                    f"[{index:>{width}}/{len(tickers)}] {outcome.ticker:<12} "
                    f"{outcome.state:<10} {outcome.detail}"
                )
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    elapsed = time.monotonic() - started
    print(
        f"done in {elapsed:.1f}s: {counts['downloaded']} downloaded, "
        f"{counts['skipped']} skipped, {counts['failed']} failed"
    )
    if auth_failed.is_set():
        print("refresh INVESTING_COOKIE and run again", file=sys.stderr)
        return 2
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
