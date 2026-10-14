"""Install a per-user macOS launchd agent; requires explicit user invocation."""
import os
import plistlib
import shutil
import sys
from pathlib import Path

if sys.platform != "darwin":
    raise SystemExit("This installer targets macOS only.")
executable = shutil.which("codex-relay") or shutil.which("codex-autoresume")
codex = shutil.which("codex")
if not executable or not codex:
    raise SystemExit("Install codex-relay and ensure 'codex' is on PATH first.")
uid = os.getuid()
home = Path.home()
logs = home / "Library/Logs/CodexAutoResume"
logs.mkdir(parents=True, exist_ok=True)
launch = home / "Library/LaunchAgents/com.local.codex-autoresume.plist"
launch.parent.mkdir(parents=True, exist_ok=True)
config = {
    "Label": "com.local.codex-autoresume",
    "ProgramArguments": [executable, "--codex", codex, "--backend", "desktop", "run"],
    "RunAtLoad": True,
    "KeepAlive": True,
    "StandardOutPath": str(logs / "stdout.log"),
    "StandardErrorPath": str(logs / "stderr.log"),
    "WorkingDirectory": str(home),
    "EnvironmentVariables": {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"),
        **({"CODEX_HOME": os.environ["CODEX_HOME"]} if os.environ.get("CODEX_HOME") else {}),
    },
}
launch.write_bytes(plistlib.dumps(config))
print("Created", launch)
print("To enable: launchctl bootstrap gui/" + str(uid) + " " + str(launch))
print("To disable: launchctl bootout gui/" + str(uid) + " " + str(launch))
print("Note: the Mac must be awake and connected to resume on time.")
