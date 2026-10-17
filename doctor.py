import os
import shutil
import subprocess
import sys
from pathlib import Path

from config import load_env, load_projects


def main():
    root = Path(__file__).resolve().parent
    load_env(root / ".env")
    errors = []
    if sys.version_info < (3, 11):
        errors.append("Нужен Python 3.11 или новее")
    for executable in ("git", os.environ.get("CODEX_BIN", "codex")):
        if not shutil.which(executable):
            errors.append(f"Не найден исполняемый файл: {executable}")
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        errors.append("Не заполнен TELEGRAM_BOT_TOKEN")
    if not os.environ.get("TELEGRAM_OWNER_ID", "").isdigit() or int(os.environ.get("TELEGRAM_OWNER_ID", "0")) <= 0:
        errors.append("Не заполнен числовой TELEGRAM_OWNER_ID")
    if not errors:
        try:
            result = subprocess.run([os.environ.get("CODEX_BIN", "codex"), "login", "status"], capture_output=True, timeout=15)
            if result.returncode:
                errors.append("Выполни codex login")
        except (OSError, subprocess.TimeoutExpired):
            errors.append("Не удалось проверить авторизацию Codex")
    try:
        projects = load_projects(root / "projects.json")
        if not projects:
            errors.append("В projects.json не добавлены проекты")
        for project in projects.values():
            for args in (["git", "-C", str(project.path), "rev-parse", "--show-toplevel"], ["git", "-C", str(project.path), "var", "GIT_AUTHOR_IDENT"], ["git", "-C", str(project.path), "remote", "get-url", "origin"]):
                if subprocess.run(args, capture_output=True, timeout=10).returncode:
                    errors.append(f"Проверь Git, origin и имя/email автора для {project.name}")
                    break
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        errors.append("Создай projects.json с существующими каталогами и командами проверок")
    if errors:
        print("\n".join(f"✗ {error}" for error in errors))
        return 1
    print("✓ Локальная конфигурация готова. Подключение Telegram и реальные задачи проверяются при запуске.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
