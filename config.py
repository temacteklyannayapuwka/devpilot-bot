import json
import re
import os
import shlex
from dataclasses import dataclass
from pathlib import Path


def load_env(path):
    if not Path(path).exists():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError("Неверный ключ в .env")
        parts = shlex.split(value, comments=True)
        if len(parts) > 1:
            raise ValueError("Значение в .env должно быть в кавычках")
        os.environ.setdefault(key, parts[0] if parts else "")


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    branch: str
    checks: list
    timeout: int = 1800


def load_projects(path):
    raw = json.loads(Path(path).read_text())
    projects = {}
    for name, item in raw.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,40}", name):
            raise ValueError("Неверное имя проекта")
        location = Path(item["path"]).expanduser().resolve()
        checks = item["checks"]
        branch = item.get("branch", "main")
        timeout = item.get("timeout_seconds", 1800)
        if not location.is_dir():
            raise ValueError(f"Каталог проекта {name} не найден")
        if not checks or not all(isinstance(c, list) and c and all(isinstance(a, str) and a for a in c) for c in checks):
            raise ValueError(f"Для {name} нужны команды checks в виде массивов")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/-]*", branch) or ".." in branch:
            raise ValueError("Неверная ветка проекта")
        if not isinstance(timeout, int) or not 60 <= timeout <= 14400:
            raise ValueError("Таймаут должен быть от 60 до 14400 секунд")
        projects[name] = Project(name, location, branch, checks, timeout)
    return projects
