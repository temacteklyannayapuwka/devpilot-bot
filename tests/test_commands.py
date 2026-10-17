import tempfile
import unittest
from pathlib import Path

from commands import Commands
from config import Project
from models import ModelSettings
from state import State


class CommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = State(self.root / "state.sqlite3")
        self.commands = Commands(self.state, {"demo": Project("demo", self.root, "main", [["true"]])}, ModelSettings(self.root / "settings.json"), self.root)

    def test_task_run_pause_resume(self):
        self.assertIn("#1", self.commands.handle("/task demo Fix timeout"))
        self.commands.handle("/pause")
        self.assertIn("пауза", self.commands.handle("/run demo"))
        self.assertTrue(self.state.get("paused"))
        self.commands.handle("/resume")
        self.assertFalse(self.state.get("paused"))
        self.assertEqual(self.state.claim()["task"], "Fix timeout")

    def test_invalid_arguments_do_not_mutate_queue(self):
        for command in ("/task missing hi", "/task demo", "/job x", "/schedule demo nan", "/schedule demo 0", "/schedule missing 6", "/mode bad"):
            self.assertIn("аргументы", self.commands.handle(command))
        self.assertFalse(self.state.jobs())
        self.assertFalse(self.state.schedules())
        self.assertEqual(self.state.get("mode", "review"), "review")

    def test_publish_requires_review_and_retry_creates_new_job(self):
        job_id = self.state.add("demo", "task")
        self.assertIn("Нет", self.commands.handle(f"/publish {job_id}"))
        self.state.update(job_id, status="failed")
        self.assertIn("#2", self.commands.handle(f"/retry {job_id}"))
        self.assertEqual(self.state.job(job_id)["status"], "failed")
        self.state.update(job_id, status="review", commit_sha="abc")
        self.assertIn("очередь", self.commands.handle(f"/publish {job_id}"))

    def test_schedule_and_mode_persist(self):
        self.commands.handle("/schedule demo 6")
        self.assertIn("6 ч", self.commands.handle("/schedule"))
        self.commands.handle("/schedule demo off")
        self.assertFalse(self.state.schedules()[0]["enabled"])
        self.commands.handle("/mode auto")
        self.assertEqual(State(self.state.path).get("mode"), "auto")

    def test_review_blocks_next_task_until_published(self):
        first = self.state.add("demo", "first")
        self.state.update(first, status="review", commit_sha="abc")
        self.state.add("demo", "second")
        self.state.request("demo")
        self.assertIsNone(self.state.claim())
        self.state.queue_publish(first)
        self.assertEqual(self.state.claim()["id"], first)
        self.state.update(first, status="done")
        self.assertEqual(self.state.claim()["task"], "second")
