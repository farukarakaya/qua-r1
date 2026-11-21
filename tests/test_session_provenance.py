# SPDX-License-Identifier: Apache-2.0

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from trading_r1 import research
from trading_r1.providers import ModelProfile
from trading_r1.providers.base import AssistantTurn, ToolCall
from trading_r1.runtime.session import records
from trading_r1.runtime.session.store import SessionStore


@unittest.skipUnless(shutil.which("git"), "Git is required for checkout fixtures.")
class CheckoutRevisionTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.enterContext(
            patch.dict(
                os.environ,
                {"HOME": str(self.home), "PATH": os.environ.get("PATH", os.defpath)},
                clear=True,
            )
        )
        self.root = self.home / "checkout"
        self.root.mkdir()
        self.git(self.root, "init", "--quiet", "--template=")
        (self.root / "README.md").write_text("Fixture\n", encoding="utf-8")

    @staticmethod
    def git(root, *args):
        return subprocess.check_output(
            ["git", "-C", str(root), *args], encoding="utf-8", timeout=10
        ).strip()

    def commit(self):
        self.git(self.root, "add", ".")
        self.git(
            self.root,
            "-c",
            "user.name=Test Fixture",
            "-c",
            "user.email=test@example.test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "Fixture",
        )
        return self.git(self.root, "rev-parse", "HEAD")

    @staticmethod
    def write_package(root):
        pkg = root / "trading_r1"
        pkg.mkdir(parents=True)
        (pkg / "research.py").write_text("# Fixture\n", encoding="utf-8")

    def test_checkout_and_linked_worktree_have_separate_revisions_and_dirty_states(self):
        self.write_package(self.root)
        first = self.commit()
        linked = self.home / "linked"
        self.git(self.root, "worktree", "add", "--quiet", "--detach", str(linked))
        second = self.commit()
        self.assertNotEqual(first, second)
        self.assertEqual(research._checkout_revision(self.root), (second, False))
        with patch.dict(
            os.environ,
            {"GIT_DIR": str(self.root / ".git"), "GIT_WORK_TREE": str(self.root)},
        ):
            self.assertEqual(research._checkout_revision(linked), (first, False))
            (linked / "new-file").touch()
            self.assertEqual(research._checkout_revision(linked), (first, True))
        self.assertEqual(research._checkout_revision(self.root), (second, False))

    def test_installed_package_does_not_claim_an_unrelated_git_revision(self):
        self.commit()
        with chdir(self.root):
            for root in (self.root, self.root / "site-packages"):
                self.write_package(root)
                self.assertEqual(research._checkout_revision(root), ("unknown", None))


class SessionProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_keeps_the_hash_of_each_served_recipe(self):
        home = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(chdir(home))
        overrides = home / "skills"
        self.enterContext(
            patch.dict(
                os.environ, {"TR1_HOME": str(home), "TR1_SKILLS_DIR": str(overrides)}, clear=True
            )
        )
        store = SessionStore(home / "sessions")
        store.begin("fixture", "Legacy turn", {"version": "0.1.1"}).end(None, termination="answer")

        name = "ticker-snapshot"
        bundled = Path(research.__file__).parent / "skills" / name / "SKILL.md"
        original = bundled.read_bytes()
        override = overrides / name / "SKILL.md"
        # Change the required tools without changing the recipe body.
        updated = (
            original.replace(b"\r\n", b"\n")
            .replace(b"- data_market_prices\n", b"- data_market_quotes\n", 1)
            .replace(b"\n", b"\r\n")
        )
        profile = ModelProfile(name="fixture", model="fixture", context_window=64_000)

        def load_skill(id_):
            return AssistantTurn(
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(id=id_, name="load_skill", arguments=json.dumps({"skills": [name]}))
                ],
            )

        answer = AssistantTurn(content="Fixture answer.", finish_reason="stop")
        generate = AsyncMock(side_effect=[load_skill("old"), answer, load_skill("new"), answer])
        sha = "1" * 40
        with (
            patch(
                "trading_r1.runtime.agent.provider_for",
                return_value=SimpleNamespace(generate=generate),
            ),
            patch.multiple(research, _GIT_SHA=sha, _GIT_DIRTY=True),
        ):
            await research.run_logged(
                store,
                "fixture",
                "Load the skill",
                profile=profile,
                reader_profile=profile,
                client=object(),
            )
            override.parent.mkdir(parents=True)
            override.write_bytes(updated)
            await research.run_logged(
                store,
                "fixture",
                "Reload the skill",
                profile=profile,
                reader_profile=profile,
                client=object(),
            )

        legacy, first, second = SessionStore(store.directory).turns("fixture")
        self.assertNotIn("git_sha", legacy["config"])
        served = []
        for turn in (first, second):
            self.assertEqual(turn["config"]["git_sha"], sha)
            self.assertIs(turn["config"]["git_dirty"], True)
            (call,) = turn["trajectory"]["tool_log"]
            (skill,) = call["payload"]["skills"]
            self.assertEqual(skill["name"], name)
            served.append(skill)
        old, new = served
        self.assertEqual(old["recipe"], new["recipe"])
        self.assertIn("data_market_prices", old["tools_loaded"])
        self.assertIn("data_market_quotes", new["tools_loaded"])
        self.assertNotEqual(old["sha256"], new["sha256"])
        results = store.lines("fixture", (records.TOOL_RESULT,))
        for content, skill, result in zip((original, updated), served, results, strict=True):
            expected = hashlib.sha256(content).hexdigest()
            self.assertEqual(skill["sha256"], expected)
            self.assertIn(f"sha256={expected}", result["data"]["result"])
            self.assertNotIn(str(home), result["data"]["result"])
