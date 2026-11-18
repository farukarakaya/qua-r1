# Trading-R1 manual

Each command takes `--help`, which also lists the less common commands and options.

## Commands

| Command | Does |
|---|---|
| `trading-r1 [QUESTION]` | Open the terminal. `--profile NAME`, `--mode MODE` |
| `trading-r1 serve` | Run the backend alone, in the foreground |
| `trading-r1 export ID` | Write a report. `--format pdf\|md\|html`, `-o FILE`, `--send CHANNELS` |
| `trading-r1 docs add PDF [PDF ...]` | Send new PDFs to the OCR model and save the transcripts locally |
| `trading-r1 sessions list`, `delete ID` | Your sessions; delete one here and its copy in your account |
| `trading-r1 channels add KIND`, `list`, `test NAME`, `remove NAME` | Your channels |
| `trading-r1 jobs add`, `list`, `run ID`, `runs ID`, `remove ID` | Your scheduled reports, and what each run did |
| `trading-r1 secrets set NAME`, `list`, `delete NAME` | Stored keys, passwords and tokens |
| `trading-r1 daemon install`, `status`, `logs`, `uninstall` | The background service |
| `trading-r1 sync status`, `on`, `off`, `pull [ID]` | Session sync; `pull` gets a session back from your account |

`sessions`, `jobs` and most of `sync` need a running backend: the terminal's, the service, or
`trading-r1 serve`.

## Settings

Open **Settings** from the palette.

| Setting | Does | Default |
|---|---|---|
| Verification | Check the report's figures against the records and recompute the worked-out ones. Takes about twice the time and tokens. | off |
| Source review | Check the report against its sources. Ignored when Verification is on. | off |
| Default mode | The mode the app opens in. | auto |
| Auto-compact | Compact the context before it fills the model's window. | on |
| Show reasoning | The model's thinking in the transcript. | on |
| Tool details | `brief` shows a tool call's values; `full` shows every argument by name. | brief |
| Side panel | The plan, data records and sources beside the transcript. | on |
| Also save as Markdown, HTML page | Files saved beside a report's PDF. | Markdown on, page off |
| Session sync | Copy your sessions to your Tauric account. | on |
| Remembered notes… | View or delete what the agent has noted about you. | |

## Import research PDFs

```bash
trading-r1 secrets set OPENAI_API_KEY
trading-r1 docs add ~/Downloads/report.pdf ~/Downloads/another-report.pdf
```

`docs add` needs no running backend. It sends the **entire PDF** in one request per new file
using the dedicated `gpt-ocr` profile. That profile defaults to OpenAI and `gpt-6.1-luna`;
it does not use your selected chat or web reader model. `--model` overrides the OCR model for
this import. To configure it persistently, edit `profiles.gpt-ocr` in `providers.json` or set
`TR1_GPT_OCR_MODEL`. If you set `TR1_GPT_OCR_BASE_URL` to another endpoint, supply its own
`TR1_GPT_OCR_API_KEY`; the default OpenAI key is not forwarded. Only import documents you
can share with the OCR endpoint. Each PDF must fit the 20 MiB file limit and the model's input
and output limits. The importer does not set an output-token cap. See [custom OCR endpoints](docs/custom-model-endpoints.md#pdf-ocr-uses-a-separate-endpoint)
for API requirements.

The command prints the path to `~/.trading-r1/docs/<sha256>.json` (under `TR1_HOME` if set). The JSON
contains a whole-report summary and numbered Markdown pages, including tables where the model
could read them. Both come from the same request. Re-adding identical bytes reuses the local copy,
even if the file was renamed. Older transcripts remain readable without a summary. Failed or
incomplete responses are not saved or retried automatically. The original PDF is unchanged.

### Use imported reports in research

Open the `/` palette and select **New session with PDFs…**. Then ask a normal research question,
such as “Which Turkish stocks have the strongest analyst recommendations?” You do not need to name
a PDF. The agent sees report summaries automatically and can use `read_doc` to open and cite pages.
The catalog says when it cannot fit all reports. Large transcripts can be read in page ranges.
Check material figures against the original PDF; OCR can misread tables and numbers.

The selected model receives the summary catalog and any page text it reads. Document sessions
are not enrolled in session sync; model requests and results are still logged locally. Use a
regular new session to ask without the library. For the authenticated `/research` API, use a new
`session_id` prefixed with `docs-` and reuse it for follow-ups. Other session IDs do not load PDFs.

## Reports, channels and jobs

`TR1_REPORTS_DIR` changes the reports folder. When the PDF cannot draw a report's text, the report
is saved as an HTML page.

```bash
trading-r1 channels add telegram
trading-r1 channels add email --preset gmail --to me@example.com   # asks for the app password
trading-r1 channels add folder --path briefs
trading-r1 channels test telegram
trading-r1 export SESSION_ID --format pdf -o report.pdf            # or md, html; --turn N
trading-r1 export SESSION_ID --send telegram,email
```

For Gmail, turn on 2-Step Verification and make an app password in your Google account. Other
providers take `--host`, `--port` and `--username`.

```bash
trading-r1 jobs add --name "Morning brief" --every weekdays --time 08:00 --tz America/New_York \
    --prompt "What moved my watchlist overnight? NVDA, AAPL, MSFT." --deliver email,telegram
trading-r1 jobs add --name "Weekly review" --every week --weekday fri --time 17:00 \
    --prompt "Review my portfolio's week." --deliver email
trading-r1 jobs list
```

`--cron` takes any five-field expression, and `--at 2027-01-04T08:00:00-05:00` runs once.
`--profile` and `--mode` pin the model and mode. Jobs run unattended, so they cannot ask you a
question.

## Session sync

Sync is on by default with a Tauric key, whichever model you use: your sessions, with their
questions, model requests and responses and tool results, are copied to your account at
`api.tauric.ai`. Turn it off under *Settings > Session sync* or with `trading-r1 sync off`.

## Borsa İstanbul market data

From a source checkout, launch with `uv run trading-r1`. This uses
our pinned borsapy build with reporting-group cache keys and Istanbul corporate-action dates.
The public PyPI borsapy build is not supported for this adapter. Ask for `XIST:THYAO` or
`THYAO.IS` in the price tools. borsapy uses TradingView for daily OHLCV in TRY, excluding
the current Istanbul calendar date. Weekly
and monthly bars are resampled; intraday bars are not available. Stock closes are split-adjusted
only, so returns exclude cash dividends. Tauric continues to serve U.S. stocks and other
existing instruments; failed BIST requests do not switch providers.

For BIST quotes, use `data.market.quotes`. For recent KAP company disclosures, use
`data.market.news` with the qualified symbol. These are official disclosures, not general
press coverage or a historical news archive. Use `XIST:<name>` in symbol search to look up
BIST companies. Known indices such as `XIST:XU100` also support daily price history.

`data.market.bist` serves BIST company profiles, financial statements, dividends, capital
changes (including paid rights), holders, analyst targets, expected filing windows, KAP
disclosure text, index constituents, and stock screening. For example, request
`symbol="XIST:THYAO", dataset="income_statement", cadence="quarterly"`. For disclosure text,
pass `dataset="disclosure"` and the numeric `disclosure_id` without a symbol. Financial statement
line items are as reported by İş Yatırım, not normalized SEC data. For banks, select
`financial_group="UFRS"`; bank-format cash flow is not available. Expected filing windows
are not confirmed earnings release dates. The Tauric equity tools direct qualified BIST
requests to the BIST tools instead of querying Tauric.

Use daily bars for cross-market baskets. If any symbol fails, the whole basket fails.
Alignment matches dates, not closing times, and leaves prices in their listing currencies.
Convert to a common currency before optimizing.

### Use IstFin alongside borsapy

Set `ISTFIN_API_KEY` on the backend (or use `uv run trading-r1 secrets set ISTFIN_API_KEY`),
then launch the terminal with `uv run trading-r1` and ask your question. New ordinary chats
include IstFin alongside borsapy and Tauric when the key is configured. These chats have
archive sync off from the first turn, even if you never call IstFin.
Without a key, new chats keep the usual settings. Resuming a chat preserves its original policy.
PDF sessions, scheduled jobs and Telegram turns do not gain IstFin tools.
API clients can `POST /sessions` and pass the returned `id` to `/research` for the same default.

| Tool | Example request | What it returns |
|---|---|---|
| `data.market.istfin_news` | `symbols=["THYAO.IS"], types=["FR"]` | One page of KAP filings with UTC publication times and a cursor. Date filters use Istanbul days. |
| `data.market.istfin_disclosure` | `disclosure_id` from a search result | KAP content blocks, correction flags and attachment links. Attachments are not downloaded. |
| `data.market.istfin_ratios` | `symbol="XIST:THYAO", metrics=["pe","pb"]` | Daily valuation multiples with the reporting period and source used on each day. |
| `data.market.istfin_index` | `symbol="XIST:XU100"` | Current constituents, percentage weights and membership date, not a historical universe. |

History and quotas depend on the subscription. IstFin is never a price or backtest feed here:
its historical bars can be unadjusted across bonus issues. borsapy still serves BIST price
history and structured corporate actions. Failed requests do not switch providers.

Use an IstFin session for personal research only. The selected model receives the data it
reads; the session is logged locally but is not synced to Tauric. Like other chats, it can
read and save personal preferences in shared notes. Keep market data out of those notes.
Settings > Remembered notes lets you view or delete notes, not turn memory on or off.
Do not share licensed data in exported reports or channels. See [IstFin's terms](https://api.istfin.com/yasal/kullanim-kosullari).

## Your data and keys

- `trading-r1 secrets set` and the key forms store keys in `~/.trading-r1/credentials.json`, and
  `trading-r1 daemon install` copies them to `~/.trading-r1/daemon.env`. Only you can read these
  files, but your own processes and any backup tool that copies the folder can too.
- Sessions, notes and settings are plain files under `~/.trading-r1/`.
- The backend listens on `127.0.0.1` by default, and every route but its liveness check needs a
  bearer token, kept in a file only you can read. Do not expose it to a network you do not trust.
- The agent can read web pages and call the data API on your behalf, and sends what it reads to
  the model you selected.
- Reports go to your channels through those services (your mail provider, Telegram). With `reply`
  on, the private chat paired with your Telegram bot can ask the agent questions.

## Configuration

Keys are read from the environment, then from `~/.trading-r1/credentials.json`. Settings, channels
and model profiles are in the same folder (`TR1_HOME` moves it).

| Variable | Does |
|---|---|
| `TR1_PROFILE` | The model to start on |
| `TR1_REPORTS_DIR` | The reports folder |
| `TR1_SKILLS_DIR` | One more folder of skills |
| `TR1_PORT` | The backend's port |
| `TR1_VLLM_BASE_URL` | Your vLLM server's address, such as `http://gpu-box:8000/v1` |
| `TR1_<NAME>_BASE_URL`, `TR1_<NAME>_MODEL`, `TR1_<NAME>_API_KEY` | One model's address, model and key |

To add a model, give it a profile in `~/.trading-r1/providers.json`:

```json
{"profiles": {"my-qwen": {"extends": "vllm", "base_url": "http://gpu-box:9000/v1",
                          "reader_profile": "my-qwen"}}}
```

Profiles with their own address send no key unless they name one (`"api_key_env": "MY_KEY"`). Those
that extend a hosted profile keep its key, so name the key they should send. For a standalone
gateway profile and a ready-to-copy example, see [Custom model endpoints](docs/custom-model-endpoints.md).

## Adding a skill

Skills are `SKILL.md` files that tell the agent how to do a kind of work with its tools. Put one in
`./skills/my-recipe/SKILL.md` under the folder you start from, or in `TR1_SKILLS_DIR`. Skills with a
built-in's name replace it.
