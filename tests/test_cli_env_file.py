# SPDX-License-Identifier: Apache-2.0

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from trading_r1.cli import main
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
            self.assertEqual(profile.base_url, "https://gateway.example/v1")
            self.assertEqual(profile.resolve_api_key(), "shell-key")
            self.assertEqual(get_profile("gpt-reader").base_url, profile.base_url)
        launch.assert_called_once()
        self.assertEqual(result.exception.code, 0)

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
                "OPENAI_BASE_URL": "https://gateway.example/v1",
                "TR1_GPT_MODEL": "main-model",
                "TR1_GPT_BASE_URL": "https://main.example/v1",
            },
        )
        self.assertEqual(profiles["gpt"].model, "main-model")
        self.assertEqual(profiles["gpt"].base_url, "https://main.example/v1")
        self.assertEqual(profiles["gpt-reader"].base_url, "https://reader.example/v1")
        self.assertEqual(profiles["gpt-ocr"].base_url, "https://api.openai.com/v1")
