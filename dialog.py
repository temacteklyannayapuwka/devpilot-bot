import asyncio
import json
import os
import signal
import time
from pathlib import Path

from codex_limits import LimitsUnavailable, codex_request


async def session_list(project):
    result = await codex_request("thread/list", {"cwd": str(project.path), "limit": 30})
    return result.get("data", [])


async def validate_session(project, thread_id):
    result = await codex_request("thread/read", {"threadId": thread_id, "includeTurns": False})
    thread = result["thread"]
    if not thread.get("cwd") or Path(thread["cwd"]).resolve() != project.path.resolve():
        raise LimitsUnavailable("Каталог диалога не совпадает с каталогом проекта.")
    if thread.get("status", {}).get("type") == "active":
        raise LimitsUnavailable("Диалог сейчас занят. Дождись завершения текущего ответа Codex.")
    return thread


async def recent_messages(thread_id):
    result = await codex_request("thread/turns/list", {"threadId": thread_id, "limit": 10, "sortDirection": "desc", "itemsView": "full"})
    messages = []
    for turn in reversed(result.get("data", [])):
        if turn.get("status") != "completed":
            continue
        for item in turn.get("items", []):
            if item.get("type") == "agentMessage" and item.get("id") and item.get("text"):
                messages.append(item)
    return messages


class SessionWatcher:
    def __init__(self, state, projects, stop):
        self.state = state
        self.projects = projects
        self.stop = stop

    def loop(self):
        while not self.stop.is_set():
            for name in self.projects:
                if self.stop.is_set():
                    return
                thread_id = self.state.get(f"session:{name}")
                if not thread_id:
                    continue
                try:
                    messages = asyncio.run(recent_messages(thread_id))
                    for item in messages:
                        if self.state.mark_dialog_message(thread_id, item["id"]):
                            for index in range(0, len(item["text"]), 3400):
                                self.state.notify(f"[{name}] Codex\n{item['text'][index:index + 3400]}")
                    self.state.set(f"watch_error:{name}", False)
                except (LimitsUnavailable, OSError, ValueError, KeyError):
                    if not self.state.get(f"watch_error:{name}", False):
                        self.state.notify(f"[{name}] Не удалось обновить ответы диалога. Проверь Codex на Mac; наблюдение повторится автоматически.")
                        self.state.set(f"watch_error:{name}", True)
            self.stop.wait(30)


class Dialog:
    def __init__(self, state, job, project, settings, stop, codex_bin):
        self.state = state
        self.job = job
        self.project = project
        self.settings = settings
        self.stop = stop
        self.codex_bin = codex_bin
        self.pending = {}
        self.sent = set()
        self.turn_id = None
        self.buffer = []

    def emit(self, text):
        for index in range(0, len(text), 3400):
            self.state.notify(f"[{self.project.name}] Codex\n{text[index:index + 3400]}")

    async def run(self):
        try:
            process = await asyncio.create_subprocess_exec(
                self.codex_bin, "app-server", cwd=self.project.path,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024,
                start_new_session=True,
                env={key: value for key, value in os.environ.items() if not key.startswith("TELEGRAM_")},
            )
        except OSError:
            raise LimitsUnavailable("Codex App Server не запустился.") from None

        async def send(data):
            process.stdin.write((json.dumps(data) + "\n").encode())
            await process.stdin.drain()

        async def event():
            while True:
                if self.stop.is_set() or self.state.job(self.job["id"])["cancel"]:
                    raise LimitsUnavailable("Диалог прерван по запросу. Уже внесённые изменения сохранены.")
                if time.monotonic() > deadline:
                    raise LimitsUnavailable("Истёк таймаут ожидания ответа диалога.")
                for token, request in list(self.pending.items()):
                    answer = self.state.get(f"dialog_reply:{token}")
                    if answer is not None:
                        await send({"id": request["id"], "result": answer})
                        self.state.set(f"dialog_request:{token}", None)
                        self.state.set(f"dialog_reply:{token}", None)
                        self.pending.pop(token)
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=1)
                except TimeoutError:
                    continue
                if not line:
                    raise LimitsUnavailable("Codex App Server закрыл соединение.")
                data = json.loads(line)
                if "method" in data and "id" in data:
                    await self.server_request(data, send)
                    continue
                if data.get("method") == "serverRequest/resolved":
                    request_id = data.get("params", {}).get("requestId")
                    for token, request in list(self.pending.items()):
                        if request["id"] == request_id:
                            self.state.set(f"dialog_request:{token}", None)
                            self.pending.pop(token)
                return data

        async def request(request_id, method, params):
            await send({"id": request_id, "method": method, "params": params})
            while True:
                data = await event()
                if data.get("id") == request_id:
                    if "error" in data:
                        raise LimitsUnavailable(f"Codex отклонил {method}. Проверь доступность сессии и занятость диалога.")
                    return data["result"]
                if data.get("method") in ("item/completed", "turn/completed"):
                    self.buffer.append(data)

        deadline = time.monotonic() + self.project.timeout
        try:
            await request(1, "initialize", {"clientInfo": {"name": "devpilot_bot", "version": "0.3.0"}})
            await send({"method": "initialized", "params": {}})
            resumed = await request(2, "thread/resume", {"threadId": self.job["thread_id"], "excludeTurns": True})
            thread = resumed["thread"]
            if thread["id"] != self.job["thread_id"] or not thread.get("cwd") or Path(thread["cwd"]).resolve() != self.project.path.resolve():
                raise LimitsUnavailable("Сессия относится к другому проекту. Отправка остановлена.")
            if thread.get("status", {}).get("type") == "active":
                raise LimitsUnavailable("Диалог уже занят в другом клиенте Codex.")
            params = {
                "threadId": self.job["thread_id"],
                "input": [{"type": "text", "text": self.job["task"]}],
                "cwd": str(self.project.path), "approvalPolicy": "on-request",
                "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [str(self.project.path)], "networkAccess": False},
            }
            model = self.settings.current()
            if model:
                params["model"] = model
            result = await request(3, "turn/start", params)
            self.turn_id = result["turn"]["id"]
            self.state.notify(f"[{self.project.name}] Сообщение #{self.job['id']} отправлено в диалог {self.job['thread_id']}.")
            while True:
                data = self.buffer.pop(0) if self.buffer else await event()
                params = data.get("params", {})
                if params.get("threadId") != self.job["thread_id"]:
                    continue
                method = data.get("method")
                if method == "item/completed" and params.get("turnId") == self.turn_id:
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage" and item.get("id") not in self.sent:
                        self.sent.add(item.get("id"))
                        if item.get("text") and self.state.mark_dialog_message(self.job["thread_id"], item["id"]):
                            self.emit(item["text"])
                if method == "turn/completed" and params.get("turn", {}).get("id") == self.turn_id:
                    status = params["turn"].get("status")
                    if status != "completed":
                        raise LimitsUnavailable(f"Codex завершил ответ со статусом {status}. Проверь диалог на Mac.")
                    return
        except (OSError, ValueError, KeyError, ConnectionError):
            raise LimitsUnavailable("Ошибка протокола диалога Codex. Проверь его состояние на Mac.") from None
        finally:
            for token in self.pending:
                self.state.set(f"dialog_request:{token}", None)
                self.state.set(f"dialog_reply:{token}", None)
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()

    async def server_request(self, data, send):
        method = data["method"]
        params = data.get("params", {})
        if params.get("threadId") != self.job["thread_id"]:
            await send({"id": data["id"], "error": {"code": -32602, "message": "Wrong thread"}})
            return
        if self.turn_id and params.get("turnId") not in (None, self.turn_id):
            await send({"id": data["id"], "error": {"code": -32602, "message": "Wrong turn"}})
            return
        token = f"{self.job['id']}-{len(self.sent)}-{len(self.pending)}-{time.time_ns()}"
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            request = {"id": data["id"], "kind": "approval", "project": self.project.name, "job_id": self.job["id"]}
            description = params.get("command") or params.get("reason") or "Изменение файлов"
            self.emit(f"Запрос разрешения:\n{str(description)[:2000]}\n/approve {token}\n/deny {token}")
        elif method == "item/tool/requestUserInput":
            questions = params.get("questions", [])
            request = {"id": data["id"], "kind": "questions", "questions": questions, "answers": {}, "project": self.project.name, "job_id": self.job["id"]}
            lines = ["Codex ждёт ответ:"]
            for question in questions:
                lines.append(f"{question['id']}: {question['question']}")
                lines.extend(f"• {option['label']}" for option in question.get("options") or [])
            lines.append(f"/answer {token} ID_ВОПРОСА ваш ответ")
            self.emit("\n".join(lines))
        else:
            await send({"id": data["id"], "error": {"code": -32601, "message": "Unsupported interactive request"}})
            self.emit("Codex запросил действие, которое этот клиент не поддерживает. Проверь диалог на Mac.")
            return
        self.pending[token] = request
        self.state.set(f"dialog_request:{token}", request)
