import tempfile
import time
import unittest
from pathlib import Path

from state import State


class StateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "state.sqlite3"
        self.state = State(self.path)

    def test_queue_claims_once_and_persists(self):
        job_id = self.state.add("demo", "task")
        self.assertIsNone(self.state.claim())
        self.assertEqual(self.state.request("demo"), job_id)
        self.assertEqual(self.state.claim()["status"], "running")
        self.assertIsNone(self.state.claim())
        self.assertEqual(State(self.path).job(job_id)["status"], "running")

    def test_schedule_uses_only_one_task_and_skips_backlog(self):
        for _ in range(3):
            self.state.add("demo", "task")
        self.state.schedule("demo", 6)
        later = time.time() + 24 * 3600
        self.state.tick(later)
        self.assertEqual(sum(j["ready"] for j in self.state.jobs()), 1)
        self.assertGreater(self.state.schedules()[0]["next_run"], later)
        self.state.tick(later + 7 * 3600)
        self.assertEqual(sum(j["ready"] for j in self.state.jobs()), 1)

    def test_pause_and_review_block_scheduled_work(self):
        first = self.state.add("demo", "first")
        self.state.add("demo", "second")
        self.state.schedule("demo", 1)
        self.state.set("paused", True)
        self.state.tick(time.time() + 7200)
        self.assertFalse(any(j["ready"] for j in self.state.jobs()))
        self.state.set("paused", False)
        self.state.update(first, status="review")
        self.state.tick(time.time() + 7200)
        self.assertFalse(any(j["ready"] for j in self.state.jobs()))

    def test_recovery_pauses_and_marks_interrupted(self):
        job_id = self.state.add("demo", "task")
        self.state.request("demo")
        self.state.claim()
        self.state.recover()
        self.assertEqual(self.state.job(job_id)["status"], "interrupted")
        self.assertTrue(self.state.get("paused"))
        self.assertTrue(self.state.messages())

    def test_cancel_and_publish_transitions(self):
        job_id = self.state.add("demo", "task")
        self.assertTrue(self.state.cancel(job_id))
        self.assertEqual(self.state.job(job_id)["status"], "cancelled")
        self.assertFalse(self.state.queue_publish(job_id))
        self.state.update(job_id, status="review", commit_sha="abc")
        self.assertTrue(self.state.queue_publish(job_id))
        self.assertFalse(self.state.queue_publish(job_id))
        self.assertEqual(self.state.claim()["status"], "publishing")

    def test_updates_and_notifications_are_durable(self):
        self.assertTrue(self.state.accept_update(1))
        self.assertFalse(State(self.path).accept_update(1))
        self.state.notify("done")
        row = State(self.path).messages()[0]
        self.state.delivered(row["id"])
        self.assertFalse(self.state.messages())
