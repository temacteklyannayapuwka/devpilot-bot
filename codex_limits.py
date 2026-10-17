import asyncio
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo


class LimitsUnavailable(Exception):
    pass


async def codex_request(method, params=None):
    try:
        process = await asyncio.create_subprocess_exec(
            os.environ.get("CODEX_BIN", "codex"), "app-server",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise LimitsUnavailable("Codex CLI не найден. Проверь CODEX_BIN.") from None

    async def send(payload):
        process.stdin.write((json.dumps(payload) + "\n").encode())
        await process.stdin.drain()

    async def response(request_id):
        while True:
            line = await process.stdout.readline()
            if not line:
                raise LimitsUnavailable("Codex App Server завершил соединение.")
            data = json.loads(line)
            if data.get("id") != request_id:
                continue
            if "error" in data:
                raise LimitsUnavailable("Запрос Codex отклонён. Проверь codex login на Mac.")
            return data["result"]

    async def exchange():
        await send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "devpilot_bot", "version": "0.1.0"}
        }})
        await response(1)
        await send({"method": "initialized", "params": {}})
        await send({"id": 2, "method": method, "params": params or {}})
        return await response(2)

    try:
        return await asyncio.wait_for(exchange(), timeout=20)
    except (TimeoutError, ValueError, KeyError, ConnectionError):
        raise LimitsUnavailable("Не удалось получить данные Codex.") from None
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()


async def read_limits():
    return await codex_request("account/rateLimits/read")


def format_limits(data, timezone="Europe/Moscow"):
    zone = ZoneInfo(timezone)
    buckets = data.get("rateLimitsByLimitId") or {}
    if not buckets and data.get("rateLimits"):
        bucket = data["rateLimits"]
        buckets = {bucket.get("limitId") or "codex": bucket}
    lines = ["Лимиты Codex"]
    found = False
    for key, bucket in buckets.items():
        lines.append(str(bucket.get("limitName") or key))
        for name in ("primary", "secondary"):
            window = bucket.get(name)
            if not window or window.get("usedPercent") is None:
                continue
            found = True
            remaining = max(0, min(100, 100 - window["usedPercent"]))
            minutes = window.get("windowDurationMins")
            duration = f"{minutes} мин" if minutes is not None else name
            reset = window.get("resetsAt")
            reset_text = datetime.fromtimestamp(reset, zone).strftime("%d.%m %H:%M") if reset else "неизвестно"
            lines.append(f"Окно {duration}: осталось {remaining:g}%; сброс {reset_text}")
    if not found:
        lines.append("Данные об остатке не предоставлены.")
    lines.append(f"Обновлено: {datetime.now(zone):%d.%m %H:%M:%S} ({timezone})")
    return "\n".join(lines)[:4000]


def limits_text():
    try:
        return format_limits(asyncio.run(read_limits()), os.environ.get("TIMEZONE", "Europe/Moscow"))
    except LimitsUnavailable as error:
        return f"Лимиты Codex недоступны.\n{error}"
