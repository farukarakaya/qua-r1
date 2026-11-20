# AGENTS.md

Instructions for coding agents working in `qua-r1`.

## Repository overview

`qua-r1` is a quantitative research fork of Tauric Research's `Trading-R1` (`origin`: `farukarakaya/qua-r1`, `upstream`: `TauricResearch/Trading-R1`).

- Python package: `trading_r1/` (console script `trading-r1 = "trading_r1.cli:main"` in `pyproject.toml`, Python >= 3.12)
- Terminal UI: `tui/` (Bun >= 1.3, OpenTUI, SolidJS; bundled into wheels at `trading_r1/_tui/`)
- Offline tests: `tests/` (`unittest`, mocked/fixture-based, no network calls or API keys)
- Utility scripts: `scripts/` (e.g. `scripts/fetch_bist_reports.py`, using `bist_stocks.txt` and gitignored `custom_report_feed/`)

## Commands and checks

Run from the repository root (see `Makefile` and `.github/workflows/ci.yml`):

```sh
uv sync --locked           # install Python dependencies
make test                  # uv run --locked python -m unittest discover -s tests
make lint                  # ruff check, ruff format --check, and tui Biome format check
make smoke                 # CLI help checks, isolated /livez backend probe, tui bun install + tsc --noEmit
make dev                   # uv run trading-r1 serve --reload
```

If Bun is not installed globally, `README.md` documents running `npm exec --yes --package=bun@1.4.4 -- make smoke`.

Tooling configs:
- Python formatting and linting: `pyproject.toml` (`[tool.ruff]`, `line-length = 100`, `target-version = "py312"`, `select = ["E", "F", "I", "UP", "B", "SIM"]`).
- TUI formatting: `tui/biome.json` (`indentWidth: 2`, `lineWidth: 110`, `semicolons: "asNeeded"`, `quoteStyle: "double"`, `trailingCommas: "all"`, excludes `src/lib/events.gen.ts`).
- TUI typecheck: `tui/tsconfig.json` (`strict: true`, `noUnusedLocals: true`, `noUnusedParameters: true`).

## Architecture and dependency boundaries

Imports flow downward (`CONTRIBUTING.md`). Preserve these layer boundaries:

1. `trading_r1/contract/` (`spec.py`, `args.py`, `units.py`, `render.py`, `events.py`): `ToolSpec`, `Result`, `ToolError`, `Output`, `Series`, `Provenance`, schema rendering, and SSE wire models. Imports neither `runtime` nor `tools`.
2. `trading_r1/state/` and `trading_r1/providers/`: disk state (`paths.py` defaults to `~/.trading-r1/`, overridden by `TR1_HOME`; `reports_dir.py` uses `TR1_REPORTS_DIR`; `documents.py` reads imported PDF transcripts and builds the bounded catalog), settings, credentials, runtime config, and model protocol adapters (`chat`, `openai-responses`, `gemini`, `anthropic`).
3. `trading_r1/runtime/`: `Agent` loop, `dispatch.py` (never-raise tool execution), `chaining.py` (`prev:<id>.<path>` resolution), `tool_registry.py`, `skill_registry.py`, `modes.py`, `taxonomy.py`, `prompts.py`, `evidence.py`, compaction/recovery, and `session/`. **`runtime` must not import `tools`.**
4. `trading_r1/tools/`:
   - `core/`: `update_plan`, `ask_user`, `evidence`, `load_tool`, `load_skill`, `remember`/`forget`, `read_doc` (when PDF access is on).
   - `web/`: `search`, `visit`, `compose_citations` bound per run to a `FetchCache`.
   - `data/`: market prices, quotes, news, symbol search, BIST (`borsapy`), IstFin (KAP and valuation ratios), macro series, and upstream Tauric tools.
   - `compute/`: pure-math analytics (`arithmetic`, `valuation`, `modeling`, `risk`, `derivatives`, `fixed_income`, `portfolio`, `statistics`, `technical`). Keep compute tools free of network I/O and data-source coupling.
5. `trading_r1/assembly.py` and `trading_r1/research.py`: wire registries, modes, skills, and providers into `run_research` and `run_logged`.
6. Top-level subsystems: `service/` (FastAPI loopback server with bearer auth), `host/` (instance lock and daemon), `scheduler/`, `delivery/` (email, Telegram, folder), `sync/`, `report/` (PDF/HTML/Markdown), `documents.py` (`trading-r1 docs` local PDF OCR store using the `gpt-ocr` profile), and `cli/`.
7. `tui/src/`: SolidJS + OpenTUI frontend speaking HTTP+SSE to `/research` and `/health`.

## Fork-specific invariants

- **No silent fallback:** When a configured provider cannot serve a request, fail explicitly with `ToolError` rather than falling back to another provider.
- **BIST routing (`trading_r1/tools/data/bist.py`, `pricing.py`, `market/bist_data.py`):** BIST tickers (`XIST:THYAO` or `THYAO.IS`) route through `borsapy` in `TRY` (split-adjusted daily/weekly/monthly bars; intraday bars are rejected for BIST). `data.market.aligned_prices` joins strictly on shared dates and fails the whole basket if any symbol fails.
- **PDF OCR and document sessions (`documents.py`, `state/documents.py`, `tools/core/read_doc.py`):** `trading-r1 docs add` uses the dedicated `gpt-ocr` profile (`openai-responses`, `ocr` role) and stores transcripts under `$TR1_HOME/docs/<sha256>.json`. PDF access is opt-in, defaults to on for `docs-` sessions, and permanently excludes the session from sync once enabled.
- **IstFin (`tools/data/market/istfin.py`):** Opt-in BIST research for KAP disclosures and valuation ratios when configured.

## Product decisions

Ask the maintainer before choosing an unspecified user-facing default, making a feature opt-in, or changing provider availability, sync, or shared memory. Explain the tradeoff and wait for confirmation. If the maintainer has already specified the behavior, implement it without changing unrelated defaults.

## Coding conventions

- **Minimal implementations:** Always use the most minimal implementation that solves the task cleanly and readably. No overbuilding, no overengineering, no speculative abstractions, and no slop.
- **File headers:** Open source files with the copyright and SPDX lines (`# Copyright 2026 Tauric Research` and `# SPDX-License-Identifier: Apache-2.0` for Python, `//` for TypeScript; test/fork modules at minimum keep `# SPDX-License-Identifier: Apache-2.0`).
- **Tool specs (`trading_r1/contract/spec.py`):** Use `Output` or `Series` subclasses (`extra="forbid"`) with types from `trading_r1/contract/units.py`, return `Result(output, Provenance(...))`, and raise `ToolError` for expected failures. Never echo raw API responses that might contain credentials or upstream errors verbatim.
- **Taxonomy (`trading_r1/runtime/taxonomy.py`):** Every tool `display_name` must match `family.name` (`web.*`, `core.*`) or `family.domain.name` (`data.<domain>.*`, `compute.<domain>.*`) using a domain in `DOMAIN_SPECS`, with a matching underscored wire `name`. Skills in `trading_r1/skills/<name>/SKILL.md` must declare a valid `domain` and `requires_tools` in YAML frontmatter.
- **Generated files (do not edit):** `tui/src/lib/events.gen.ts` (generated from `trading_r1/contract/events.py`) and `assets/taxonomy.svg`.
- **Comments and copy:** Comments explain what the code does or invariants a reader must know, not change history. Keep user-facing text, tool errors, and documentation short and plain, with no em dash, en dash, or middle dot.
- **Secrets and paths:** Never commit credentials, `.env` files, or machine-specific paths and hosts.

## Testing and reviews

- **Meaningful tests only:** Tests must usefully verify actual business logic, invariants, and edge cases in our code. Do not add tests that do not genuinely test our logic.
- **Keep tests offline:** Mock HTTP/provider transports (`httpx`, `borsapy`, `OpenAI`, etc.) and use temporary `TR1_HOME` directories.
- **Reviews:** Check for correctness, completeness, and code quality: clean, readable code with no overbuilt logic, no slop, and no slop tests.

## Verification before finishing

1. Run `make test` and `make lint` after edits (and `make smoke` if CLI, FastAPI startup, packaging, or `tui/` TypeScript changed).
2. Target `farukarakaya/qua-r1` (`main`) for fork PRs and keep one focused change per PR.
