<p align="center">
  <a href="https://tauric.ai/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/Trading-R1-dark.svg">
      <img src="assets/Trading-R1.svg" alt="Trading-R1" width="60%">
    </picture>
  </a>
</p>

<p align="center">
  <a href="https://arxiv.org/abs/2509.11420"><img alt="arXiv" src="https://img.shields.io/badge/arXiv-2509.11420-B31B1B?logo=arxiv"/></a>
  <a href="https://discord.com/invite/hk9PGKShPK"><img alt="Discord" src="https://img.shields.io/badge/Discord-TauricResearch-7289da?logo=discord&amp;logoColor=white"/></a>
  <a href="https://x.com/TauricResearch"><img alt="X" src="https://img.shields.io/badge/X-TauricResearch-white?logo=x&amp;logoColor=white"/></a>
  <a href="https://github.com/TauricResearch/"><img alt="GitHub" src="https://img.shields.io/badge/GitHub-TauricResearch-14C290?logo=github&amp;logoColor=white"/></a>
  <a href="https://tauric.ai/"><img alt="Website" src="https://img.shields.io/badge/Website-tauric.ai-14C290?logo=data:image/svg%2bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0ibm9uZSIgc3Ryb2tlPSIjZmZmIiBzdHJva2Utd2lkdGg9IjIiPjxjaXJjbGUgY3g9IjEyIiBjeT0iMTIiIHI9IjEwIi8+PHBhdGggZD0iTTIgMTJoMjBNMTIgMmExNSAxNSAwIDAgMSAwIDIwTTEyIDJhMTUgMTUgMCAwIDAgMCAyMCIvPjwvc3ZnPg=="/></a>
</p>

<p align="center">
  <a href="https://tauricresearch.github.io/Trading-R1/">English</a> |
  <a href="https://tauricresearch.github.io/Trading-R1/de/">Deutsch</a> |
  <a href="https://tauricresearch.github.io/Trading-R1/es/">Español</a> |
  <a href="https://tauricresearch.github.io/Trading-R1/fr/">français</a> |
  <a href="https://tauricresearch.github.io/Trading-R1/ja/">日本語</a> |
  <a href="https://tauricresearch.github.io/Trading-R1/ko/">한국어</a> |
  <a href="https://tauricresearch.github.io/Trading-R1/pt/">Português</a> |
  <a href="https://tauricresearch.github.io/Trading-R1/ru/">Русский</a> |
  <a href="https://tauricresearch.github.io/Trading-R1/zh/">中文</a>
</p>

<h2 align="center">Trading-R1: The terminal for financial research and analysis</h2>

<p align="center">
  <img src="assets/trading-r1.gif" alt="Trading-R1 answering a research question in the terminal" width="100%">
</p>

## Quick start

To run the qua-r1 fork, use a source checkout with [uv](https://docs.astral.sh/uv/), Python 3.12+, and [Bun](https://bun.sh/) 1.3+ for the terminal UI. The checkout uses the fork's pinned borsapy build and exposes the `trading-r1` command.

```sh
git clone https://github.com/farukarakaya/qua-r1.git
cd qua-r1
uv sync --locked
uv run trading-r1
```

No API key is needed to open the terminal UI. For Tauric model or data access, [sign up](https://tauric.ai/signup), [create a key](https://tauric.ai/settings/api-keys), and run `uv run trading-r1 secrets set TAURIC_API_KEY`. You can also set `TAURIC_API_KEY` in the backend's environment.

Tauric is the default source for macro data and U.S. market data; no data-mode setting is needed. There are no direct FRED/ALFRED or TCMB EVDS3 adapters. For BIST price history and optional IstFin tools, see the [manual](MANUAL.md).

## Choose a model

| Name | Model | Needs |
|---|---|---|
| `tauric` | `tauric` | `TAURIC_API_KEY` |
| `gpt` | `gpt-6.1-sol` | `OPENAI_API_KEY` |
| `anthropic` | `claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |
| `gemini` | `gemini-3.8-flash` | `GEMINI_API_KEY` |
| `vllm` | `Qwen3.8-27B` | a vLLM server you run |

The app starts on the first you have set up, in the order OpenAI, Anthropic, Gemini, Tauric, vLLM.
Change it with *Model…* in the palette, or start on one with `uv run trading-r1 --profile gpt`. To point
`vllm` at your server, see [Configuration](MANUAL.md#configuration). For other OpenAI-compatible
servers and gateways, see [Custom model endpoints](docs/custom-model-endpoints.md).

## Use it

```bash
uv run trading-r1                                          # open the terminal
uv run trading-r1 -e .env                                  # load settings from .env
uv run trading-r1 "What's NVDA's last quarterly revenue?"  # and ask a first question
uv run trading-r1 --mode analytical                        # start in a mode
```

| Key | Does |
|---|---|
| `/` or `^K` | Open the command palette |
| `shift+tab` | Cycle the mode |
| `^N` | Start new research |
| `esc` | Cancel the running turn, or close a dialog |
| `^C` | Quit |

| Mode | Focus |
|---|---|
| `auto` | Full toolkit; the agent decides |
| `research` | Web first, for deep research |
| `analytical` | Numbers first: data and valuation tools, with the web for context |

The palette lists every action: sessions, model and mode, reports, channels, scheduled reports,
tools and skills, API keys, the background service, Settings and Help.

## Architecture

<p align="center">
  <img src="assets/taxonomy.svg" alt="Trading-R1's tools: those always loaded, the finance tools by discipline and domain, and the workflow skills" width="100%">
</p>

## Reports and sending

*Export report…* saves the last answer as a PDF and, by default, Markdown, in
`~/Documents/Trading-R1` or `~/Trading-R1`.

*Send report…* sends it to your channels, and sets one up if there is none.

| Channel | You give | Gets |
|---|---|---|
| `telegram` | a bot token from @BotFather, then write to the bot once and confirm the chat | text and the PDF |
| `email` | your address and an app password (presets: `gmail`, `icloud`, `yahoo`, `fastmail`) | text and the PDF |
| `folder` | a path | the report files |

To ask from your phone, set the Telegram channel's `reply` to `on`. While the backend is up, your
bot answers your messages with a short research run, and `/new` starts over.

## Scheduled reports

Jobs ask a question on a schedule and send the report to your channels. Use *Scheduled reports…* in
the palette, or:

```bash
uv run trading-r1 jobs add --name "Morning brief" --every weekdays --time 08:00 --tz America/New_York \
    --prompt "What moved my watchlist overnight? NVDA, AAPL, MSFT." --deliver email,telegram
```

The commands and options for reports, channels and jobs are in the
[manual](MANUAL.md#reports-channels-and-jobs).

## Background service

Jobs and Telegram replies need a running backend, and the terminal's stops when you close it. To
keep one running, install a user service (Linux with systemd, macOS with launchd), from *Background
service…* in the palette or:

```bash
uv run trading-r1 daemon install  # also: status, logs, start, stop, restart, uninstall
```

After you update, or change a key in your shell profile, run `uv run trading-r1 daemon install` again.

## Local development

From the repository root:

```sh
uv sync --locked
make test     # Offline Python tests
make smoke    # CLI version/help, backend /livez, and TUI typecheck
make lint     # Ruff and Biome checks
```

No API keys are needed. `make smoke` and `make lint` need Bun. If you have npm but no `bun` on `PATH`, run `npm exec --yes --package=bun@1.4.4 -- make smoke`. The same prefix works for `make lint` and `uv run trading-r1`.

### Test the backend with auto-reload

```sh
dev_home=$(mktemp -d)
TR1_HOME="$dev_home" TR1_PORT=8765 make dev
```

In another terminal, run `curl -fsS http://127.0.0.1:8765/livez`. A response with `"status":"ok"` confirms the server is up, not that providers are configured. The reload server binds to loopback and skips authentication and the scheduler. Stop it with `Ctrl+C` before deleting the temporary state directory. To test normal backend behavior, run `uv run trading-r1 serve` instead.

### Test the terminal UI

Run `uv run trading-r1` to start or reuse a regular backend. To attach to the reload server instead, run `TR1_API_URL=http://127.0.0.1:8765 uv run trading-r1` while `make dev` is running. For an actual research prompt, configure a model first, for example with `uv run trading-r1 secrets set OPENAI_API_KEY`, then select it via *Model…*. Use the same `TR1_HOME` when storing credentials for an isolated backend.

## Manual

Commands, settings, session sync, your data and keys, configuration and adding a skill are in the
[user manual](MANUAL.md). To work on the code, see [CONTRIBUTING](CONTRIBUTING.md).

## Citation

```bibtex
@article{xiao2025trading,
  title={Trading-r1: Financial trading with llm reasoning via reinforcement learning},
  author={Xiao, Yijia and Sun, Edward and Chen, Tong and Wu, Fang and Luo, Di and Wang, Wei},
  journal={arXiv preprint arXiv:2509.11420},
  year={2025}
}
```

## Disclaimer

For research and education only, not investment advice.

## License

Apache-2.0. See [LICENSE](LICENSE).
