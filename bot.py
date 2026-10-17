import json
import logging
import os
import time
import urllib.error
import urllib.request

from codex_limits import limits_text


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
        except (urllib.error.URLError, TimeoutError, OSError):
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
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    try:
        owner_id = int(os.environ.get("TELEGRAM_OWNER_ID", "0"))
        interval = int(os.environ.get("LIMITS_INTERVAL_SECONDS", "300"))
        if not token or owner_id <= 0 or interval < 60:
            raise ValueError
    except ValueError:
        raise SystemExit("Задай TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID и интервал >= 60 секунд.")
    telegram = Telegram(token)
    offset = 0
    panel_id = None
    next_refresh = float("inf")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.info("DevPilot запущен")
    while True:
        try:
            updates = telegram.call("getUpdates", offset=offset, timeout=20, allowed_updates=["message"])
            for update in updates:
                message = update.get("message", {})
                if authorized(message, owner_id):
                    command = message.get("text", "").split(maxsplit=1)[0:1]
                    command = command[0].split("@", 1)[0] if command else ""
                    if command in ("/start", "/limits"):
                        panel = telegram.call("sendMessage", chat_id=owner_id, text=limits_text())
                        panel_id = panel["message_id"]
                        next_refresh = time.monotonic() + interval
                    elif command == "/status":
                        telegram.call("sendMessage", chat_id=owner_id, text=(
                            "DevPilot работает.\n"
                            f"Панель лимитов обновляется каждые {interval} секунд после /start.\n"
                            "Выполнение задач разработки пока не подключено.\n"
                            "/limits — свежие лимиты\n/stop — остановить обновление панели"
                        ))
                    elif command == "/stop":
                        panel_id = None
                        next_refresh = float("inf")
                        telegram.call("sendMessage", chat_id=owner_id, text="Обновление панели остановлено.")
                offset = update["update_id"] + 1
            if panel_id and time.monotonic() >= next_refresh:
                text = limits_text()
                try:
                    telegram.call("editMessageText", chat_id=owner_id, message_id=panel_id, text=text)
                except TelegramError as error:
                    if error.code == 400 and "message is not modified" in error.description:
                        pass
                    elif error.code == 400 and "message to edit not found" in error.description:
                        panel = telegram.call("sendMessage", chat_id=owner_id, text=text)
                        panel_id = panel["message_id"]
                    else:
                        raise
                finally:
                    next_refresh = time.monotonic() + interval
        except TelegramError as error:
            logging.warning("Ошибка Telegram, код %s", error.code)
            if error.code in (401, 409):
                raise SystemExit("Проверь токен и убедись, что запущен только один экземпляр бота.")
            time.sleep(error.retry_after)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
