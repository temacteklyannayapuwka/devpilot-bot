import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from codex_limits import LimitsUnavailable
from commands import Commands
from config import Project
from dialog import Dialog, validate_session, recent_messages
from models import ModelSettings
from state import State


class DialogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = State(self.root / "state.sqlite3")
        self.project = Project("demo", self.root, "main", [["true"]])
        self.settings = ModelSettings(self.root / "settings.json")
        job_id = self.state.add("demo", "Продолжай", "session-123")
        self.job = self.state.claim()
        self.assertEqual(job_id, self.job["id"])
        self.dialog = Dialog(self.state, self.job, self.project, self.settings, threading.Event(), "codex")

    def process(self, messages):
        process = Mock(returncode=None, pid=99999999)
        process.stdin.drain = AsyncMock()
        process.wait = AsyncMock(return_value=0)
        process.stdout.readline = AsyncMock(side_effect=[(json.dumps(message) + "\n").encode() for message in messages])
        return process

    async def test_existing_session_receives_text_and_returns_agent_answer(self):
        self.settings.save("test-model")
        process = self.process([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "session-123", "cwd": str(self.root), "status": {"type": "idle"}}}},
            {"id": 3, "result": {"turn": {"id": "turn-1"}}},
            {"method": "item/completed", "params": {"threadId": "other-session", "turnId": "turn-1", "item": {"id": "foreign", "type": "agentMessage", "text": "wrong"}}},
            {"method": "item/completed", "params": {"threadId": "session-123", "turnId": "turn-1", "item": {"id": "message-1", "type": "agentMessage", "text": "Готово. Продолжаем?"}}},
            {"method": "turn/completed", "params": {"threadId": "session-123", "turn": {"id": "turn-1", "status": "completed"}}},
        ])
        with patch("dialog.asyncio.create_subprocess_exec", AsyncMock(return_value=process)), patch("dialog.os.killpg"):
            await self.dialog.run()
        sent = [json.loads(call.args[0]) for call in process.stdin.write.call_args_list]
        self.assertEqual([row.get("method") for row in sent], ["initialize", "initialized", "thread/resume", "turn/start"])
        self.assertEqual(sent[-1]["params"]["threadId"], "session-123")
        self.assertEqual(sent[-1]["params"]["input"][0]["text"], "Продолжай")
        self.assertEqual(sent[-1]["params"]["model"], "test-model")
        output = "\n".join(row["text"] for row in self.state.messages())
        self.assertIn("Готово. Продолжаем?", output)
        self.assertNotIn("wrong", output)

    async def test_wrong_project_is_rejected_before_turn_start(self):
        process = self.process([
            {"id": 1, "result": {}},
            {"id": 2, "result": {"thread": {"id": "session-123", "cwd": "/another/project"}}},
        ])
        with patch("dialog.asyncio.create_subprocess_exec", AsyncMock(return_value=process)), patch("dialog.os.killpg"):
            with self.assertRaises(LimitsUnavailable):
                await self.dialog.run()
        methods = [json.loads(call.args[0]).get("method") for call in process.stdin.write.call_args_list]
        self.assertNotIn("turn/start", methods)

    async def test_approval_requires_explicit_owner_command(self):
        await self.dialog.server_request({"id": "request-1", "method": "item/commandExecution/requestApproval", "params": {"threadId": "session-123", "command": "git push"}}, AsyncMock())
        token, _ = self.state.pending_requests("demo")[0]
        commands = Commands(self.state, {"demo": self.project}, self.settings, self.root)
        commands.handle(f"/deny {token}")
        self.assertEqual(self.state.get(f"dialog_reply:{token}"), {"decision": "decline"})

    async def test_plain_text_answers_single_pending_question(self):
        await self.dialog.server_request({"id": "request-1", "method": "item/tool/requestUserInput", "params": {"threadId": "session-123", "questions": [{"id": "scope", "question": "Какой вариант?"}]}}, AsyncMock())
        token, _ = self.state.pending_requests("demo")[0]
        self.state.set("active_project", "demo")
        commands = Commands(self.state, {"demo": self.project}, self.settings, self.root)
        commands.handle("Первый вариант")
        self.assertEqual(self.state.get(f"dialog_reply:{token}"), {"answers": {"scope": {"answers": ["Первый вариант"]}}})
        self.assertEqual(len(self.state.jobs()), 1)

    async def test_bind_validation_rejects_active_session(self):
        with patch("dialog.codex_request", AsyncMock(return_value={"thread": {"cwd": str(self.root), "status": {"type": "active"}}})):
            with self.assertRaises(LimitsUnavailable):
                await validate_session(self.project, "session-123")

    async def test_history_watch_uses_only_completed_agent_messages(self):
        with patch("dialog.codex_request", AsyncMock(return_value={"data": [
            {"status": "completed", "items": [{"id": "new", "type": "agentMessage", "text": "New answer"}, {"id": "tool", "type": "commandExecution", "text": "private log"}]},
            {"status": "inProgress", "items": [{"id": "partial", "type": "agentMessage", "text": "Partial"}]},
            {"status": "completed", "items": [{"id": "old", "type": "agentMessage", "text": "Old answer"}]},
        ]})):
            result = await recent_messages("session-123")
        self.assertEqual([item["id"] for item in result], ["old", "new"])

    async def test_wrong_thread_approval_is_not_presented(self):
        send = AsyncMock()
        await self.dialog.server_request({"id": "request-1", "method": "item/commandExecution/requestApproval", "params": {"threadId": "other", "command": "git push"}}, send)
        self.assertIn("error", send.call_args.args[0])
        self.assertFalse(self.state.pending_requests("demo"))

    async def test_stream_and_history_share_message_deduplication(self):
        self.assertTrue(self.state.mark_dialog_message("session-123", "message"))
        self.assertFalse(self.state.mark_dialog_message("session-123", "message"))
        self.assertTrue(self.state.mark_dialog_message("other-session", "message"))


class ProjectSwitchTests(unittest.TestCase):
    def test_switching_projects_does_not_retarget_queued_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = State(root / "state.sqlite3")
            projects = {name: Project(name, root, "main", [["true"]]) for name in ("java", "go")}
            commands = Commands(state, projects, ModelSettings(root / "settings.json"), root)
            state.set("session:java", "java-session")
            state.set("session:go", "go-session")
            commands.handle("/use java")
            commands.handle("Продолжай Java")
            commands.handle("/use go")
            commands.handle("Продолжай Go")
            first, second = list(reversed(state.jobs()))
            self.assertEqual((first["project"], first["thread_id"]), ("java", "java-session"))
            self.assertEqual((second["project"], second["thread_id"]), ("go", "go-session"))
            commands.handle("/leave")
            self.assertIn("Выбери", commands.handle("hello"))
