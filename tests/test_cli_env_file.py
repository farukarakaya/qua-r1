# SPDX-License-Identifier: Apache-2.0

import io
import os
import plistlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

from trading_r1.cli import main
from trading_r1.host import daemon
from trading_r1.providers import get_profile, load_profiles
from trading_r1.state.paths import state_dir


class TestEnvFile(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env_file = self.directory / ".env"

    def test_terminal_loads_state_and_model_configuration(self):
        home = self.directory / ".qua-r1"
        shell_home = Path.home()
        self.env_file.write_text(
            f'TR1_HOME="$HOME/{os.path.relpath(home, shell_home)}"\n'
            "QUA_MODEL=fixture-model\n"
            "QUA_REASONING_EFFORT=medium\n"
            "OPENAI_BASE_URL=https://gateway.example/v1\n"
            "OPENAI_API_KEY=file-key\n"
        )
        with (
            patch.dict(
                os.environ, {"HOME": str(shell_home), "OPENAI_API_KEY": "shell-key"}, clear=True
            ),
            patch("sys.argv", ["trading-r1", "-e", str(self.env_file)]),
            patch("trading_r1.cli.terminal._tui", return_value=0) as launch,
        ):
            with self.assertRaises(SystemExit) as result:
                main()
            self.assertEqual(state_dir(), home)
            profile = get_profile()
            self.assertEqual(profile.model, "fixture-model")
            self.assertEqual(profile.reasoning_effort, "medium")
            self.assertEqual(profile.base_url, "https://gateway.example/v1")
            self.assertEqual(profile.resolve_api_key(), "shell-key")
            reader = get_profile("gpt-reader")
            self.assertEqual(reader.base_url, profile.base_url)
            self.assertIsNone(reader.reasoning_effort)
            self.assertIsNone(get_profile("gpt-ocr").reasoning_effort)
        launch.assert_called_once()
        self.assertEqual(result.exception.code, 0)

    def test_installed_daemon_uses_foreground_gpt_configuration(self):
        env = {
            "TR1_HOME": str(self.directory),
            "OPENAI_API_KEY": "fixture-key",
            "OPENAI_BASE_URL": "https://gateway.example/v1",
            "QUA_MODEL": "fixture-model",
            "QUA_REASONING_EFFORT": "low",
        }
        for manager in (daemon.SystemdUser(self.directory), daemon.Launchd(self.directory)):
            with (
                self.subTest(manager=type(manager).__name__),
                patch.dict(os.environ, env, clear=True),
            ):
                foreground = get_profile("gpt")
                daemon.install(
                    manager,
                    "trading-r1",
                    8765,
                    env,
                    run=lambda cmd: subprocess.CompletedProcess(cmd, 0),
                )
                if manager.writes_env_file:
                    installed = dotenv_values(self.directory / daemon.ENV_FILE)
                else:
                    installed = plistlib.loads(manager.unit_path.read_bytes())[
                        "EnvironmentVariables"
                    ]
                with patch.dict(os.environ, installed, clear=True):
                    profile = get_profile("gpt")
                    self.assertEqual(profile.base_url, foreground.base_url)
                    self.assertEqual(profile.model, foreground.model)
                    self.assertEqual(profile.reasoning_effort, foreground.reasoning_effort)
                    self.assertEqual(profile.resolve_api_key(), env["OPENAI_API_KEY"])

    def test_missing_file_fails_before_launch(self):
        stderr = io.StringIO()
        with (
            patch("sys.argv", ["trading-r1", "--env-file", str(self.env_file)]),
            patch("trading_r1.cli.terminal._tui") as launch,
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as result,
        ):
            main()
        launch.assert_not_called()
        self.assertEqual(result.exception.code, 2)
        self.assertIn("cannot read environment file", stderr.getvalue())

    def test_profile_specific_settings_take_precedence(self):
        providers = self.directory / "providers.json"
        providers.write_text('{"profiles":{"gpt-reader":{"base_url":"https://reader.example/v1"}}}')
        profiles = load_profiles(
            path=providers,
            env={
                "QUA_MODEL": "fixture-model",
                "QUA_REASONING_EFFORT": "low",
                "OPENAI_BASE_URL": "https://gateway.example/v1",
                "TR1_GPT_MODEL": "main-model",
                "TR1_GPT_BASE_URL": "https://main.example/v1",
                "TR1_GPT_REASONING_EFFORT": " Medium ",
                "TR1_GPT_READER_REASONING_EFFORT": "none",
            },
        )
        self.assertEqual(profiles["gpt"].model, "main-model")
        self.assertEqual(profiles["gpt"].base_url, "https://main.example/v1")
        self.assertEqual(profiles["gpt"].reasoning_effort, "medium")
        self.assertEqual(profiles["gpt-reader"].base_url, "https://reader.example/v1")
        self.assertEqual(profiles["gpt-reader"].reasoning_effort, "none")
        self.assertEqual(profiles["gpt-ocr"].base_url, "https://api.openai.com/v1")
        self.assertIsNone(profiles["gpt-ocr"].reasoning_effort)
