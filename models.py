import asyncio
import json
import os
import tempfile
from pathlib import Path

from codex_limits import LimitsUnavailable, codex_request


class SettingsError(Exception):
    pass


class ModelSettings:
    def __init__(self, path=None):
        self.path = Path(path) if path else Path(__file__).resolve().parent / ".devpilot" / "settings.json"

    def current(self):
        try:
            data = json.loads(self.path.read_text())
            model = data.get("model")
            if model is not None and not isinstance(model, str):
                raise ValueError
            return model
        except FileNotFoundError:
            return None
        except (OSError, ValueError, AttributeError):
            raise SettingsError("Не удалось прочитать настройки модели.") from None

    def save(self, model):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as stream:
                temporary = stream.name
                json.dump({"model": model}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise SettingsError("Не удалось сохранить модель; выбор не подтверждён.") from None
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


async def available_models():
    models = []
    cursor = None
    seen = set()
    while True:
        params = {"limit": 100, "includeHidden": False}
        if cursor:
            params["cursor"] = cursor
        result = await codex_request("model/list", params)
        for item in result.get("data", []):
            if item.get("model") and not item.get("hidden", False):
                models.append(item)
        cursor = result.get("nextCursor")
        if not cursor:
            return models
        if cursor in seen:
            raise LimitsUnavailable("Не удалось загрузить полный список моделей.")
        seen.add(cursor)


def model_command(command, argument, settings):
    try:
        if command == "/model" and not argument:
            selected = settings.current() or "по умолчанию Codex"
            return f"Выбрана модель: {selected}\n/models — список\n/model ИМЯ — выбрать\n/model default — сбросить"
        if command == "/model" and argument == "default":
            settings.save(None)
            return "Выбор сброшен: для будущих задач будет использоваться модель по умолчанию Codex."
        models = asyncio.run(available_models())
        if command == "/models":
            if not models:
                return "Codex не предоставил доступные модели."
            selected = settings.current()
            lines = ["Доступные модели:"]
            for item in models:
                marker = " ✓" if item["model"] == selected else ""
                lines.append(f"{item['model']}{marker}")
            lines.append("Выбор: /model ИМЯ\nНастройка предназначена для будущих задач DevPilot.")
            return "\n".join(lines)
        if argument not in {item["model"] for item in models}:
            return "Модель отсутствует в доступном списке. Посмотри /models."
        settings.save(argument)
        return f"Сохранена модель: {argument}\nВыбор предназначен для будущих задач DevPilot. Текущие чаты Codex не меняются."
    except (LimitsUnavailable, SettingsError) as error:
        return str(error)
