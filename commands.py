from datetime import datetime
from zoneinfo import ZoneInfo

from models import model_command


HELP = """DevPilot
/projects — проекты
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
            return self.dispatch(command, argument)
        except (ValueError, KeyError):
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
            return "\n".join(f"{p.name} → {p.path} → origin/{p.branch}" for p in self.projects.values()) or "Нет проектов. Заполни projects.json на Mac."
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
                new_id = self.state.add(job["project"], job["task"])
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
