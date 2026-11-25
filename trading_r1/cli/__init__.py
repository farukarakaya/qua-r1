# Copyright 2026 Tauric Research
# SPDX-License-Identifier: Apache-2.0

"""`trading-r1`: launches the Trading-R1 terminal.

trading-r1                       # the TUI (backend + UI)
trading-r1 -e .env               # load environment variables before startup
trading-r1 "NVDA DCF value?"     # the TUI, seeded with a first question
trading-r1 --profile vllm        # the TUI on a given backbone (/model switches later)
trading-r1 serve                 # the HTTP+SSE backend alone (development)
trading-r1 export SESSION        # write a session's report to a file
trading-r1 docs add PDF          # transcribe and store a research PDF
trading-r1 jobs | channels | secrets | daemon | sync | sessions   # subcommands, in cli/*.py

``main`` is the console-script entry point. Exit status is 0 on success and non-zero when a
subcommand fails; each subcommand is a module with a ``main`` that returns its own status.
``terminal`` runs the TUI, and ``client`` is the subcommands' shared call to the running backend.
"""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path

from dotenv import load_dotenv

from trading_r1 import __version__
from trading_r1.providers import ProfileSelectionError, available_backbones
from trading_r1.runtime.modes import MODES

# The subcommands: name to module, resolved when one is run.
_SUBCOMMANDS = {
    "serve": "trading_r1.cli.backend",
    "export": "trading_r1.cli.export",
    "docs": "trading_r1.cli.documents",
    "jobs": "trading_r1.cli.jobs",
    "channels": "trading_r1.cli.channels",
    "secrets": "trading_r1.cli.credentials",
    "daemon": "trading_r1.cli.daemon",
    "sync": "trading_r1.cli.sync",
    "sessions": "trading_r1.cli.sessions",
}

_FILES = """\
files, under ~/.trading-r1 (TR1_HOME moves it):
  settings.json     what Settings saves
  credentials.json  stored keys
  providers.json    add or change a model profile, for example
                    {"profiles": {"my-qwen": {"extends": "vllm",
                     "base_url": "http://gpu-box:9000/v1", "reader_profile": "my-qwen"}}}
                    A new profile with its own address sends no key unless it names one
                    ("api_key_env": "MY_KEY"). One that extends a hosted profile keeps
                    that profile's key, so name the key it should send.
  runtime.json      limits: timeouts, retries, concurrency, token budgets, compaction

environment:
  TR1_PROFILE, TR1_REPORTS_DIR, TR1_PORT, TR1_LOG_LEVEL, TR1_LOG_FILE, TR1_SKILLS_DIR
  TR1_<NAME>_BASE_URL, TR1_<NAME>_MODEL, TR1_<NAME>_REASONING_EFFORT for a model
  QUA_MODEL, QUA_REASONING_EFFORT, OPENAI_BASE_URL for the OpenAI profile
  TR1_VLLM_READER_BASE_URL, the page reader, if on another server

Without --profile or a saved choice, the model is the first set up: an OpenAI, Anthropic, Gemini or
Tauric key, then a vLLM server (TR1_VLLM_BASE_URL) or an Ollama (TR1_OLLAMA_MODEL or
TR1_OLLAMA_BASE_URL)."""


def _load_env_file(parser: argparse.ArgumentParser, path: Path | None) -> None:
    if path is None:
        return
    existing_home = "TR1_HOME" in os.environ
    try:
        with path.expanduser().open(encoding="utf-8-sig") as stream:
            load_dotenv(stream=stream, override=False)
    except (OSError, ValueError):
        parser.error(f"cannot read environment file {str(path)!r}")
    if not existing_home and (home := os.environ.get("TR1_HOME")):
        # dotenv expands ${HOME}. Also accept $HOME and resolve paths before the TUI changes cwd.
        os.environ["TR1_HOME"] = str(Path(os.path.expandvars(home)).expanduser().resolve())


def main() -> None:
    """Entry point: the TUI by default; a subcommand's name runs that module's ``main``."""
    env_parser = argparse.ArgumentParser(prog="trading-r1", add_help=False, allow_abbrev=False)
    env_parser.add_argument(
        "-e", "--env-file", type=Path, metavar="FILE", help="load environment variables from FILE"
    )
    env_args, argv = env_parser.parse_known_args()
    if argv and argv[0] in _SUBCOMMANDS:
        _load_env_file(env_parser, env_args.env_file)
        module = importlib.import_module(_SUBCOMMANDS[argv[0]])
        raise SystemExit(module.main(argv[1:]))

    ap = argparse.ArgumentParser(
        prog="trading-r1",
        description="Trading-R1 terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="also: trading-r1 " + " | ".join(_SUBCOMMANDS) + " (each takes --help)\n\n" + _FILES,
        parents=[env_parser],
    )
    ap.add_argument("--version", action="version", version=f"trading-r1 {__version__}")
    ap.add_argument("question", nargs="?", help="a first question to ask")
    ap.add_argument(
        "--mode",
        choices=list(MODES),
        default=None,
        help="the starting execution mode (default: auto). auto = web + tools; "
        "analytical = numbers-first.",
    )
    ap.add_argument(
        "--profile",
        default=None,
        help="the model to start on (tauric, gpt, anthropic, gemini, vllm, ollama); "
        "default: the saved Model… choice, else the first with its key set.",
    )
    args = ap.parse_args()
    _load_env_file(ap, args.env_file)
    try:
        backbones = [b["name"] for b in available_backbones()]
    except ProfileSelectionError as err:
        ap.error(str(err))
    if args.profile and args.profile not in backbones:
        ap.error(f"unknown --profile {args.profile!r}; backbones: {', '.join(backbones)}")
    if (named := os.environ.get("TR1_PROFILE")) and named not in backbones:
        ap.error(f"unknown TR1_PROFILE {named!r}; backbones: {', '.join(backbones)}")
    from trading_r1.cli import terminal

    raise SystemExit(
        terminal._tui(initial_prompt=args.question, profile=args.profile, mode=args.mode)
    )


if __name__ == "__main__":
    main()
