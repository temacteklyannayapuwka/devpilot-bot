import argparse
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path


LABEL = "local.devpilot.bot"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["install", "uninstall", "status"])
    args = parser.parse_args()
    if sys.platform != "darwin":
        raise SystemExit("Эта команда предназначена для macOS.")
    root = Path(__file__).resolve().parent
    destination = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if args.action == "status":
        raise SystemExit(subprocess.call(["launchctl", "print", f"{domain}/{LABEL}"]))
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    if args.action == "uninstall":
        destination.unlink(missing_ok=True)
        print("Автозапуск удалён.")
        return
    if not (root / ".env").is_file():
        raise SystemExit("Сначала создай и заполни .env.")
    directory = root / ".devpilot"
    directory.mkdir(mode=0o700, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    executable = shutil.which("caffeinate") or "/usr/bin/caffeinate"
    payload = {
        "Label": LABEL,
        "ProgramArguments": [executable, "-i", sys.executable, str(root / "bot.py")],
        "WorkingDirectory": str(root), "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 30,
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(directory / "service.log"),
        "StandardErrorPath": str(directory / "service-error.log"),
    }
    with destination.open("wb") as stream:
        plistlib.dump(payload, stream)
    os.chmod(destination, 0o600)
    subprocess.run(["launchctl", "bootstrap", domain, str(destination)], check=True)
    print("DevPilot установлен в автозапуск. /start в Telegram.")


if __name__ == "__main__":
    main()
