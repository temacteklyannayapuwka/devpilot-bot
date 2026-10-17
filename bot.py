import json
import logging
import os
import time
import urllib.error
import urllib.request
import fcntl
import signal
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

from codex_limits import limits_text
from models import ModelSettings
from config import load_env, load_projects
from commands import Commands, HELP
from runner import Runner
from state import State


class TelegramError(Exception):
    def __init__(self, code, description, retry_after=5):
        self.code = code
        self.description = description
        self.retry_after = min(60, max(1, retry_after))


class Telegram:
    def __init__(self, token):
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **params):
        request = urllib.request.Request(
            self.base_url + method,
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=35) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                data = json.load(error)
            except ValueError:
                raise TelegramError(error.code, "HTTP error") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise TelegramError(0, "Network error") from None
        if not data.get("ok"):
            raise TelegramError(
                data.get("error_code", 0), data.get("description", "Unknown error"),
                data.get("parameters", {}).get("retry_after", 5),
            )
        return data["result"]


def authorized(message, owner_id):
    return (
        message.get("from", {}).get("id") == owner_id
        and message.get("chat", {}).get("type") == "private"
        and message.get("chat", {}).get("id") == owner_id
    )


def main():
    root = Path(__file__).resolve().parent
    load_env(root / ".env")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    try:
        owner_id = int(os.environ.get("TELEGRAM_OWNER_ID", "0"))
        interval = int(os.environ.get("LIMITS_INTERVAL_SECONDS", "300"))
        if not token or owner_id <= 0 or interval < 60:
            raise ValueError
    except ValueError:
        raise SystemExit("Задай TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID и интервал >= 60 секунд.")
    telegram = Telegram(token)
    directory = root / ".devpilot"
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    singleton = (directory / "bot.lock").open("a")
    try:
        fcntl.flock(singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("DevPilot уже запущен.") from None
    timezone = os.environ.get("TIMEZONE", "Europe/Moscow")
    ZoneInfo(timezone)
    config_path = root / "projects.json"
    projects = load_projects(config_path) if config_path.exists() else {}
    state = State(directory / "state.sqlite3")
    state.recover()
    settings = ModelSettings(directory / "settings.json")
    commands = Commands(state, projects, settings, directory / "runs", timezone)
    runner = Runner(state, projects, settings, directory / "runs", os.environ.get("CODEX_BIN", "codex"))
    offset = state.get("offset", 0)
    panel_id = state.get("panel_id")
    next_refresh = time.monotonic() if panel_id else float("inf")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.info("DevPilot запущен")
    worker = threading.Thread(target=runner.loop, name="devpilot-worker", daemon=True)
    worker.start()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: runner.stop.set())
    try:
        run_loop(telegram, owner_id, interval, state, commands, runner, offset, panel_id, next_refresh)
    finally:
        runner.stop.set()
        worker.join(timeout=70)
        singleton.close()


def send_text(telegram, owner_id, text):
    for index in range(0, len(text), 3500):
        telegram.call("sendMessage", chat_id=owner_id, text=text[index:index + 3500])


def run_loop(telegram, owner_id, interval, state, commands, runner, offset, panel_id, next_refresh):
    while not runner.stop.is_set():
        try:
            for message in state.messages():
                send_text(telegram, owner_id, message["text"])
                state.delivered(message["id"])
            updates = telegram.call("getUpdates", offset=offset, timeout=5, allowed_updates=["message"])
            for update in updates:
                message = update.get("message", {})
                if authorized(message, owner_id) and state.accept_update(update["update_id"]):
                    command = message.get("text", "").split(maxsplit=1)[0:1]
                    command = command[0].split("@", 1)[0] if command else ""
                    if command in ("/start", "/limits"):
                        if command == "/start":
                            telegram.call("sendMessage", chat_id=owner_id, text=HELP, reply_markup={
                                "keyboard": [["/status", "/projects"], ["/tasks", "/schedule"], ["/pause", "/resume"], ["/limits", "/models"]],
                                "resize_keyboard": True,
                            })
                        panel = telegram.call("sendMessage", chat_id=owner_id, text=limits_text())
                        panel_id = panel["message_id"]
                        state.set("panel_id", panel_id)
                        next_refresh = time.monotonic() + interval
                    elif command == "/stop":
                        panel_id = None
                        state.set("panel_id", None)
                        next_refresh = float("inf")
                        state.notify("Обновление панели остановлено.")
                    else:
                        text = commands.handle(message.get("text", ""))
                        for index in range(0, len(text), 3500):
                            state.notify(text[index:index + 3500])
                offset = update["update_id"] + 1
                state.set("offset", offset)
            if panel_id and time.monotonic() >= next_refresh:
                text = limits_text()
                try:
                    telegram.call("editMessageText", chat_id=owner_id, message_id=panel_id, text=text)
                except TelegramError as error:
                    if error.code == 400 and "message is not modified" in error.description:
                        pass
                    elif error.code == 400 and ("message to edit not found" in error.description or "message can't be edited" in error.description):
                        panel = telegram.call("sendMessage", chat_id=owner_id, text=text)
                        panel_id = panel["message_id"]
                        state.set("panel_id", panel_id)
                    else:
                        raise
                finally:
                    next_refresh = time.monotonic() + interval
        except TelegramError as error:
            logging.warning("Ошибка Telegram, код %s", error.code)
            if error.code in (401, 409):
                raise SystemExit("Проверь токен и убедись, что запущен только один экземпляр бота.")
            runner.stop.wait(error.retry_after)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
