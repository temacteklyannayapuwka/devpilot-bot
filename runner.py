import fcntl
import os
import signal
import re
import subprocess
import threading
import time
from pathlib import Path

from models import SettingsError


class RunError(Exception):
    pass


class Cancelled(RunError):
    pass


def child_environment():
    return {key: value for key, value in os.environ.items() if not key.startswith("TELEGRAM_")}


class Runner:
    def __init__(self, state, projects, settings, directory, codex_bin="codex"):
        self.state = state
        self.projects = projects
        self.settings = settings
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.codex_bin = codex_bin
        self.stop = threading.Event()

    def command(self, args, cwd, log, timeout=120, job_id=None, input_text=None):
        started = time.monotonic()
        with log.open("ab") as output:
            process = subprocess.Popen(
                args, cwd=cwd, env=child_environment(), stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            )
            try:
                if input_text is not None:
                    process.stdin.write(input_text.encode())
                    process.stdin.close()
                while process.poll() is None:
                    if self.stop.is_set() or (job_id and self.state.job(job_id)["cancel"]):
                        raise Cancelled("Выполнение отменено; изменения сохранены в отдельном рабочем дереве.")
                    if time.monotonic() - started > timeout:
                        raise RunError("Превышен таймаут команды. См. локальный журнал.")
                    self.stop.wait(0.2)
                if process.returncode:
                    raise RunError(f"Команда {Path(args[0]).name} завершилась с кодом {process.returncode}. См. локальный журнал.")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()

    def git(self, path, *args):
        if self.stop.is_set():
            raise Cancelled("DevPilot завершает работу.")
        result = subprocess.run(
            ["git", "-C", str(path), *args], capture_output=True, text=True,
            timeout=60, env=child_environment(),
        )
        if result.returncode:
            raise RunError("Ошибка Git: " + " ".join(args[:2]) + ". Проверь авторизацию, ветку и локальный журнал.")
        return result.stdout.strip()

    def paths(self, job_id):
        home = self.directory / str(job_id)
        home.mkdir(parents=True, exist_ok=True)
        return home, home / "worktree", home / "run.log"

    def publish(self, job, project, tree, log):
        sha = self.git(tree, "rev-parse", "HEAD")
        if sha != job["commit_sha"] or self.git(tree, "status", "--porcelain"):
            raise RunError("Коммит или рабочее дерево изменились после проверки. Публикация остановлена.")
        self.command(["git", "push", "origin", f"{sha}:refs/heads/{project.branch}"], tree, log)
        self.state.update(job["id"], status="done", report=job["report"] + f"\nОпубликовано в origin/{project.branch}.")

    def execute(self, job):
        project = self.projects.get(job["project"])
        if project is None:
            raise RunError("Проект отсутствует в projects.json.")
        home, tree, log = self.paths(job["id"])
        common = Path(self.git(project.path, "rev-parse", "--git-common-dir"))
        if not common.is_absolute():
            common = project.path / common
        with (common / "devpilot.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RunError("Проект уже занят другим DevPilot.") from None
            if job["status"] == "publishing":
                self.publish(job, project, tree, log)
                return
            if self.git(project.path, "status", "--porcelain"):
                raise RunError("В основном каталоге проекта есть незакоммиченные изменения.")
            self.command(["git", "fetch", "origin", project.branch], project.path, log)
            base = self.git(project.path, "rev-parse", "FETCH_HEAD")
            branch = f"devpilot/task-{job['id']}"
            self.state.update(job["id"], branch=branch)
            self.git(project.path, "worktree", "add", "-b", branch, str(tree), base)
            model = self.settings.current()
            self.state.notify(f"▶ Задача #{job['id']} · {project.name}\nМодель: {model or 'по умолчанию Codex'}\n{job['task'][:500]}")
            prompt = (
                "Implement exactly this one task in the current repository. Inspect current code first. "
                "Do not create additional features, commits, branches, push, deploy, or change Git configuration. "
                "Do not add explanatory code comments, TODOs, roadmaps or future-step hints. "
                "README should describe only purpose, current features and running instructions. "
                "Never write secrets. Run relevant checks. Finish with a concise summary in Russian.\n\nTask:\n"
                + job["task"]
            )
            args = [self.codex_bin, "--ask-for-approval", "never", "exec", "--sandbox", "workspace-write"]
            if model:
                args += ["--model", model]
            args += ["-C", str(tree), prompt]
            self.command(args, tree, log, project.timeout, job["id"])
            if self.git(tree, "rev-parse", "HEAD") != base:
                raise RunError("Codex самостоятельно изменил историю Git. Нужна ручная проверка.")
            if not self.git(tree, "status", "--porcelain"):
                self.state.update(job["id"], status="no_changes", report="Codex не внёс изменений; коммит не создавался.")
                return
            for check in project.checks:
                self.command(check, tree, log, project.timeout, job["id"])
            if self.git(tree, "rev-parse", "HEAD") != base:
                raise RunError("История Git изменилась во время проверок.")
            if self.state.job(job["id"])["cancel"] or self.stop.is_set():
                raise Cancelled("Отменено до коммита.")
            self.git(tree, "add", "--all")
            staged = self.git(tree, "diff", "--cached", "--name-only", "-z").split("\0")
            forbidden = {".env", "auth.json", "credentials.json", "id_rsa", "id_ed25519"}
            if any(Path(name).name in forbidden or (Path(name).name.startswith(".env.") and Path(name).name != ".env.example") or name.endswith((".pem", ".key", ".p12")) for name in staged):
                raise RunError("В изменениях обнаружен файл с возможными секретами. Коммит остановлен.")
            if not self.git(tree, "diff", "--cached", "--stat"):
                self.state.update(job["id"], status="no_changes", report="После проверок изменений нет.")
                return
            title = job["task"].splitlines()[0][:90]
            self.git(tree, "commit", "-m", f"feat: {title}")
            sha = self.git(tree, "rev-parse", "HEAD")
            report = f"Проверки: {len(project.checks)} успешно.\nКоммит: {sha}\nВетка: {branch}\n" + self.git(tree, "show", "--stat", "--format=", "HEAD")[:1500]
            remote = self.git(tree, "remote", "get-url", "origin")
            match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?", remote)
            if match:
                report += f"\nhttps://github.com/{match[1]}/commit/{sha}"
            self.state.update(job["id"], status="review", commit_sha=sha, report=report)
            if self.state.get("mode", "review") == "auto" and not self.stop.is_set() and not self.state.job(job["id"])["cancel"]:
                self.publish(self.state.job(job["id"]), project, tree, log)

    def loop(self):
        while not self.stop.is_set():
            job = None
            try:
                self.state.tick()
                if self.state.get("paused", False):
                    self.stop.wait(1)
                    continue
                job = self.state.claim()
                if job:
                    try:
                        self.execute(job)
                    except (RunError, OSError, subprocess.SubprocessError, SettingsError) as error:
                        current = self.state.job(job["id"])
                        status = "review" if current["commit_sha"] else ("cancelled" if isinstance(error, Cancelled) else "failed")
                        self.state.update(job["id"], status=status, report=current["report"] + "\n" + str(error))
                    result = self.state.job(job["id"])
                    if result["status"] == "failed":
                        self.state.set("paused", True)
                        self.state.notify("Новые запуски приостановлены после ошибки. Проверь /jobs, затем /resume.")
                    self.state.notify(f"Задача #{job['id']} · {job['project']} · {result['status']}\n{result['report']}\n/job {job['id']}")
            except Exception:
                if job:
                    self.state.update(job["id"], status="failed", report="Внутренняя ошибка. Проверь локальные файлы задачи.")
                self.state.set("paused", True)
                self.state.notify("DevPilot приостановлен из-за внутренней ошибки. Проверь журнал и /jobs.")
            self.stop.wait(1)
