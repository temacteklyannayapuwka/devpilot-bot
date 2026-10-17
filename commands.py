from datetime import datetime
from zoneinfo import ZoneInfo
import asyncio
import subprocess

from models import model_command
from codex_limits import LimitsUnavailable
from dialog import session_list, validate_session, recent_messages


HELP = """DevPilot
/projects — проекты
/use ПРОЕКТ — выбрать проект для переписки
/project [ПРОЕКТ] — состояние проекта
/sessions ПРОЕКТ — доступные диалоги Codex
/bind ПРОЕКТ ID — привязать существующий диалог
/leave — выйти из режима переписки
/approve ТОКЕН / /deny ТОКЕН — запрос разрешения
/answer ТОКЕН ID_ВОПРОСА ответ — ответ на вопрос Codex
/task ПРОЕКТ описание — поставить задачу
/tasks [ПРОЕКТ] — очередь и история
/run ПРОЕКТ — запустить следующую задачу
/job ID — отчёт, коммит, журнал
/cancel ID — отменить задачу
/retry ID — создать повторную задачу
/publish ID — отправить проверенный коммит в основную ветку
/reject ID — отклонить результат
/schedule ПРОЕКТ ЧАСЫ — интервал 1–168 часов
/schedule ПРОЕКТ off — отключить
/schedule — расписание
/pause — приостановить новые запуски
/resume — продолжить
/mode [review|auto] — публикация с подтверждением или автоматически
/models — модели
/model [ИМЯ|default] — выбор модели
/limits — панель лимитов
/stop — остановить панель лимитов
/status — состояние
/help — команды"""


class Commands:
    def __init__(self, state, projects, settings, directory, timezone="Europe/Moscow"):
        self.state = state
        self.projects = projects
        self.settings = settings
        self.directory = directory
        self.zone = ZoneInfo(timezone)

    def handle(self, text):
        parts = text.strip().split(maxsplit=1)
        if not parts:
            return HELP
        command = parts[0].split("@", 1)[0]
        argument = parts[1].strip() if len(parts) > 1 else ""
        try:
            if not text.lstrip().startswith("/"):
                name = self.state.get("active_project")
                if not name:
                    return "Выбери проект: /projects, затем /use ИМЯ."
                self.project(name)
                requests = [(token, request) for token, request in self.state.pending_requests(name) if request["kind"] == "questions"]
                if requests:
                    token, request = requests[0]
                    unanswered = [q for q in request["questions"] if q["id"] not in request["answers"]]
                    if len(requests) == 1 and len(unanswered) == 1:
                        return self.dispatch("/answer", f"{token} {unanswered[0]['id']} {text}")
                    return "Codex задал несколько вопросов. Ответь командой /answer из его сообщения."
                thread_id = self.state.get(f"session:{name}")
                if not thread_id:
                    return f"Сначала привяжи диалог: /sessions {name}, затем /bind {name} ID."
                job_id = self.state.add(name, text, thread_id)
                return f"[{name}] Сообщение #{job_id} в очереди." + (" Включена пауза: /resume." if self.state.get("paused", False) else "")
            return self.dispatch(command, argument)
        except LimitsUnavailable as error:
            return str(error)
        except (ValueError, KeyError, IndexError):
            return "Проверь аргументы команды. /help"

    def project(self, name):
        if name not in self.projects:
            raise ValueError("Unknown project")
        return self.projects[name]

    def dispatch(self, command, argument):
        if command in ("/help", "/start"):
            return HELP
        if command in ("/models", "/model"):
            return model_command(command, argument, self.settings)
        if command == "/projects":
            active = self.state.get("active_project")
            return "\n".join(f"{'✓ ' if p.name == active else ''}{p.name} → {p.path}\nДиалог: {self.state.get('session:' + p.name) or 'не привязан'}\n/use {p.name}" for p in self.projects.values()) or "Нет проектов. Заполни projects.json на Mac."
        if command in ("/use", "/chat"):
            self.project(argument)
            self.state.set("active_project", argument)
            return f"Выбран {argument}. Обычные сообщения пойдут в его диалог Codex." if self.state.get(f"session:{argument}") else f"Выбран {argument}. Привяжи диалог: /sessions {argument}, затем /bind {argument} ID."
        if command == "/leave":
            self.state.set("active_project", None)
            return "Режим переписки отключён. Задачи и расписание продолжают работать."
        if command == "/sessions":
            project = self.project(argument)
            sessions = asyncio.run(session_list(project))
            return "\n".join(f"{s['id']} · {s.get('name') or s.get('preview', '')[:120]}" for s in sessions) or "Codex CLI не вернул диалоги этого каталога. Если у тебя есть ID доступной локальной сессии, используй /bind. Облачные чаты автоматически не переносятся."
        if command == "/bind":
            name, thread_id = argument.split()
            project = self.project(name)
            asyncio.run(validate_session(project, thread_id))
            for item in asyncio.run(recent_messages(thread_id)):
                self.state.mark_dialog_message(thread_id, item["id"])
            self.state.set(f"session:{name}", thread_id)
            self.state.set("active_project", name)
            return f"Диалог {thread_id} привязан к {name}. Пиши обычным сообщением — передам в Codex."
        if command == "/project":
            name = argument or self.state.get("active_project")
            project = self.project(name)
            try:
                result = subprocess.run(["git", "-C", str(project.path), "status", "--short", "--branch"], capture_output=True, text=True, timeout=10)
                if result.returncode:
                    return "Не удалось прочитать состояние Git проекта."
                return f"{name}\n{project.path}\nДиалог: {self.state.get('session:' + name) or 'не привязан'}\n{result.stdout[:2500]}"
            except (OSError, subprocess.TimeoutExpired):
                return "Не удалось прочитать состояние Git проекта."
        if command in ("/approve", "/deny", "/answer"):
            parts = argument.split(maxsplit=2)
            token = parts[0]
            request = self.state.get(f"dialog_request:{token}")
            if not request or self.state.job(request["job_id"])["status"] != "running":
                return "Запрос уже завершён или не найден."
            if self.state.get(f"dialog_reply:{token}") is not None:
                return "Ответ уже передан; дождись продолжения Codex."
            if command in ("/approve", "/deny") and request["kind"] == "approval":
                result = {"decision": "accept" if command == "/approve" else "decline"}
            elif command == "/answer" and request["kind"] == "questions":
                _, question_id, answer = parts
                valid = {q["id"] for q in request["questions"]}
                if question_id not in valid:
                    return "Неизвестный ID вопроса."
                request["answers"][question_id] = {"answers": [answer]}
                self.state.set(f"dialog_request:{token}", request)
                if valid - request["answers"].keys():
                    return "Ответ сохранён. Остались вопросы: " + ", ".join(sorted(valid - request["answers"].keys()))
                result = {"answers": request["answers"]}
            else:
                return "Команда не соответствует типу запроса."
            self.state.set(f"dialog_reply:{token}", result)
            return f"[{request['project']}] Ответ передан Codex."
        if command == "/task":
            name, task = argument.split(maxsplit=1)
            self.project(name)
            if len(task) > 6000:
                return "Описание задачи должно быть не длиннее 6000 символов."
            job_id = self.state.add(name, task)
            return f"Задача #{job_id} добавлена. /run {name}"
        if command in ("/tasks", "/jobs", "/commits"):
            if argument:
                self.project(argument)
            jobs = self.state.jobs(argument or None)
            if command == "/commits":
                jobs = [job for job in jobs if job["commit_sha"]]
            return "\n".join(f"#{j['id']} {j['project']} · {j['status']} · {j['task'][:100]}" for j in jobs) or "Задач пока нет."
        if command == "/run":
            self.project(argument)
            job_id = self.state.request(argument)
            return f"Задача #{job_id} готова к запуску." + (" Сейчас включена пауза." if self.state.get("paused", False) else "") if job_id else "Нет ожидающих задач."
        if command == "/pause":
            self.state.set("paused", True)
            return "Новые запуски приостановлены. Активную задачу останавливает /cancel ID."
        if command == "/resume":
            self.state.set("paused", False)
            return "Новые запуски разрешены."
        if command == "/mode":
            if argument:
                if argument not in ("auto", "review"):
                    raise ValueError
                self.state.set("mode", argument)
            mode = self.state.get("mode", "review")
            return f"Режим: {mode}. " + ("Новые результаты публикуются в основную ветку после проверок." if mode == "auto" else "После проверок коммит ждёт /publish ID.")
        if command == "/schedule":
            if argument:
                name, hours = argument.split()
                self.project(name)
                if hours == "off":
                    with self.state.db() as db:
                        db.execute("UPDATE schedules SET enabled=0 WHERE project=?", (name,))
                    return f"Расписание {name} отключено."
                value = float(hours)
                if not 1 <= value <= 168:
                    raise ValueError
                self.state.schedule(name, value)
            lines = []
            for row in self.state.schedules():
                when = datetime.fromtimestamp(row["next_run"], self.zone).strftime("%d.%m %H:%M")
                lines.append(f"{row['project']}: каждые {row['hours']:g} ч; {when}; {'включено' if row['enabled'] else 'выключено'}")
            return "\n".join(lines) or "Расписание не настроено. /schedule ПРОЕКТ 6"
        if command in ("/job", "/cancel", "/retry", "/publish", "/reject"):
            job_id = int(argument)
            job = self.state.job(job_id)
            if not job:
                return "Задача не найдена."
            if command == "/cancel":
                return "Отмена запрошена." if self.state.cancel(job_id) else "Эту задачу уже нельзя отменить."
            if command == "/retry":
                if job["status"] not in ("failed", "cancelled", "interrupted", "rejected", "no_changes"):
                    return "Повтор возможен для неуспешных, отклонённых или прерванных задач."
                new_id = self.state.add(job["project"], job["task"], job.get("thread_id"))
                return f"Создана задача #{new_id}. /run {job['project']}"
            if command == "/publish":
                return "Публикация поставлена в очередь." if self.state.queue_publish(job_id) else "Нет проверенного коммита, ожидающего публикации."
            if command == "/reject":
                return "Результат отклонён. Рабочее дерево сохранено." if self.state.discard(job_id) else "Задача не ожидает проверки."
            return f"#{job_id} {job['project']} · {job['status']}\n{job['task']}\n{job['report']}\nЖурнал: {self.directory / str(job_id) / 'run.log'}"
        if command == "/status":
            with self.state.db() as db:
                running = db.execute("SELECT id FROM jobs WHERE status IN ('running','publishing')").fetchall()
            return f"DevPilot работает.\nПауза: {'да' if self.state.get('paused', False) else 'нет'}\nРежим: {self.state.get('mode', 'review')}\nАктивные задачи: " + (", ".join(str(j["id"]) for j in running) or "нет")
        return "Неизвестная команда. /help"
