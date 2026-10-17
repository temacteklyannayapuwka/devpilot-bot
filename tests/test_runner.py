import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Project
from models import ModelSettings
from runner import Runner, RunError, Cancelled, child_environment
from state import State


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.DEVNULL, text=True).strip()


class FakeCodexRunner(Runner):
    change = True
    secret = False

    def command(self, args, cwd, log, timeout=120, job_id=None, input_text=None):
        if args[0] == "fake-codex":
            self.last_args = args
            if self.change:
                (Path(cwd) / "app.py").write_text("value = 2\n")
            if self.secret:
                (Path(cwd) / ".env").write_text("TOKEN=not-real\n")
            return
        return super().command(args, cwd, log, timeout, job_id, input_text)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote)], check=True, capture_output=True)
        self.repo = self.root / "project"
        subprocess.run(["git", "clone", str(self.remote), str(self.repo)], check=True, capture_output=True)
        git(self.repo, "config", "user.name", "DevPilot Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "checkout", "-b", "main")
        (self.repo / "app.py").write_text("value = 1\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "initial")
        git(self.repo, "push", "origin", "main")
        self.base = git(self.repo, "rev-parse", "HEAD")
        self.state = State(self.root / "state.sqlite3")
        self.settings = ModelSettings(self.root / "settings.json")
        self.project = Project("demo", self.repo, "main", [[sys.executable, "-c", "assert open('app.py').read() == 'value = 2\\n'"]])
        self.runner = FakeCodexRunner(self.state, {"demo": self.project}, self.settings, self.root / "runs", "fake-codex")
        job_id = self.state.add("demo", "change value")
        self.state.request("demo")
        self.job = self.state.claim()
        self.assertEqual(job_id, self.job["id"])

    def test_review_commit_then_publish_and_original_unchanged(self):
        self.settings.save("test-model")
        self.runner.execute(self.job)
        result = self.state.job(self.job["id"])
        self.assertEqual(result["status"], "review")
        self.assertIn("test-model", self.runner.last_args)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.base)
        self.assertEqual((self.repo / "app.py").read_text(), "value = 1\n")
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)
        self.state.queue_publish(result["id"])
        self.runner.execute(self.state.claim())
        self.assertEqual(self.state.job(result["id"])["status"], "done")
        self.assertEqual(git(self.remote, "rev-parse", "main"), result["commit_sha"])

    def test_dirty_original_is_rejected(self):
        (self.repo / "local.txt").write_text("local")
        with self.assertRaises(RunError):
            self.runner.execute(self.job)
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_failed_checks_do_not_create_commit_or_publish(self):
        self.project.checks[:] = [[sys.executable, "-c", "raise SystemExit(1)"]]
        with self.assertRaises(RunError):
            self.runner.execute(self.job)
        tree = self.runner.paths(self.job["id"])[1]
        self.assertEqual(git(tree, "rev-parse", "HEAD"), self.base)
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)

    def test_no_changes_means_no_empty_commit(self):
        self.runner.change = False
        self.runner.execute(self.job)
        self.assertEqual(self.state.job(self.job["id"])["status"], "no_changes")

    def test_secret_file_stops_commit(self):
        self.runner.secret = True
        with self.assertRaises(RunError):
            self.runner.execute(self.job)
        tree = self.runner.paths(self.job["id"])[1]
        self.assertEqual(git(tree, "rev-parse", "HEAD"), self.base)

    def test_auto_mode_pushes_after_checks(self):
        self.state.set("mode", "auto")
        self.runner.execute(self.job)
        result = self.state.job(self.job["id"])
        self.assertEqual(result["status"], "done")
        self.assertEqual(git(self.remote, "rev-parse", "main"), result["commit_sha"])

    def test_remote_conflict_never_force_pushes(self):
        self.runner.execute(self.job)
        result = self.state.job(self.job["id"])
        (self.repo / "other.txt").write_text("other")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "concurrent change")
        git(self.repo, "push", "origin", "main")
        remote_head = git(self.remote, "rev-parse", "main")
        self.state.queue_publish(result["id"])
        with self.assertRaises(RunError):
            self.runner.execute(self.state.claim())
        self.assertEqual(git(self.remote, "rev-parse", "main"), remote_head)

    def test_bot_token_is_not_passed_to_child_process(self):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "secret", "TELEGRAM_OWNER_ID": "42"}):
            environment = child_environment()
        self.assertNotIn("TELEGRAM_BOT_TOKEN", environment)
        self.assertNotIn("TELEGRAM_OWNER_ID", environment)

    def test_process_timeout(self):
        with self.assertRaises(RunError):
            self.runner.command([sys.executable, "-c", "import time; time.sleep(30)"], self.repo, self.root / "timeout.log", timeout=0.01)

    def test_cancel_terminates_running_command(self):
        self.state.cancel(self.job["id"])
        with self.assertRaises(Cancelled):
            self.runner.command([sys.executable, "-c", "import time; time.sleep(30)"], self.repo, self.root / "cancel.log", job_id=self.job["id"])

    def test_modified_review_result_cannot_be_published(self):
        self.runner.execute(self.job)
        tree = self.runner.paths(self.job["id"])[1]
        (tree / "app.py").write_text("unverified change\n")
        self.state.queue_publish(self.job["id"])
        with self.assertRaises(RunError):
            self.runner.execute(self.state.claim())
        self.assertEqual(git(self.remote, "rev-parse", "main"), self.base)
