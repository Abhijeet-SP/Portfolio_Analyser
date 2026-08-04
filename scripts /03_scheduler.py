"""
Daily unattended runner for the whole ETL pipeline.

    python3 "scripts /03_scheduler.py" install     register the 16:15 daily job
    python3 "scripts /03_scheduler.py" run         run the pipeline now
    python3 "scripts /03_scheduler.py" status      is the job registered?
    python3 "scripts /03_scheduler.py" uninstall   remove the job

Scheduling is left to launchd rather than a resident Python process: it
survives reboot and, unlike cron, still fires when the Mac was asleep at
16:15. Every run appends a one-line verdict to reports/09_scheduler_log.txt.
"""

from datetime import datetime
from pathlib import Path
import os
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]

LOAD_DATA = Path(__file__).resolve().parent / "02_load_data.py"

SCHEDULER_LOG = PROJECT_ROOT / "reports" / "09_scheduler_log.txt"

# 15 minutes after the 15:30 IST close, so the EOD prints have settled.
RUN_HOUR = 16
RUN_MINUTE = 15

LABEL = "com.arihant.portfolio_risk_analyser.daily_load"

PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"

# launchd captures the pipeline's stdout/stderr here; the run verdict itself
# goes to SCHEDULER_LOG.
STDOUT_LOG = PROJECT_ROOT / "reports" / "09_scheduler_stdout.txt"


def build_plist():
    """
    Absolute paths are baked in on purpose: launchd starts jobs with a bare
    environment and cwd=/, so nothing relative can be relied on.
    """

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{Path(__file__).resolve()}</string>
        <string>run</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{PROJECT_ROOT}</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>{RUN_HOUR}</integer>
        <key>Minute</key>
        <integer>{RUN_MINUTE}</integer>
    </dict>

    <!-- Run on wake if the machine was asleep at the scheduled time. -->
    <key>RunAtLoad</key>
    <false/>

    <key>StandardOutPath</key>
    <string>{STDOUT_LOG}</string>
    <key>StandardErrorPath</key>
    <string>{STDOUT_LOG}</string>
</dict>
</plist>
"""


def log_run(message):

    SCHEDULER_LOG.parent.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(SCHEDULER_LOG, "a") as f:
        f.write(f"{stamp} | {message}\n")


def run_pipeline():
    """
    Run load_data.py and record the verdict. Exit code is propagated so
    launchd's own accounting matches the log.
    """

    if not LOAD_DATA.exists():
        # nothing to run: say so in the log, since nobody is watching stdout
        log_run(f"FAILED | loader not found at {LOAD_DATA}")
        print(f"Loader not found : {LOAD_DATA}")
        return 1

    start = datetime.now()

    log_run("START  | daily load")

    result = subprocess.run(
        [sys.executable, str(LOAD_DATA)],
        cwd=PROJECT_ROOT,
    )

    elapsed = (datetime.now() - start).total_seconds()

    if result.returncode == 0:
        log_run(f"OK     | daily load finished in {elapsed:.0f}s")
    else:
        log_run(
            f"FAILED | exit {result.returncode} after {elapsed:.0f}s "
            f"| see reports/ for the failing stage"
        )

    return result.returncode


def install():

    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(build_plist())

    # bootout first so a re-install replaces the old definition instead of
    # erroring on an already-loaded label
    subprocess.run(
        ["launchctl", "bootout", f"gui/{os_uid()}/{LABEL}"],
        capture_output=True,
    )

    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os_uid()}", str(PLIST_PATH)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("Failed to register the job with launchd:")
        print(result.stderr.strip() or result.stdout.strip())
        return 1

    print(f"Installed : {PLIST_PATH}")
    print(f"Runs      : every day at {RUN_HOUR:02d}:{RUN_MINUTE:02d} local time")
    print(f"Verdicts  : {SCHEDULER_LOG.relative_to(PROJECT_ROOT)}")
    print(f"Output    : {STDOUT_LOG.relative_to(PROJECT_ROOT)}")

    log_run(f"INSTALL| scheduled daily at {RUN_HOUR:02d}:{RUN_MINUTE:02d}")

    return 0


def uninstall():

    subprocess.run(
        ["launchctl", "bootout", f"gui/{os_uid()}/{LABEL}"],
        capture_output=True,
    )

    if PLIST_PATH.exists():
        PLIST_PATH.unlink()

    print(f"Removed : {LABEL}")

    log_run("REMOVE | schedule uninstalled")

    return 0


def status():

    result = subprocess.run(
        ["launchctl", "list", LABEL],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"Not scheduled. Run: python3 \"{Path(__file__).name}\" install")
        return 1

    print(f"Scheduled : {LABEL}")
    print(f"Runs      : every day at {RUN_HOUR:02d}:{RUN_MINUTE:02d} local time")
    print(result.stdout.strip())

    return 0


def os_uid():
    return os.getuid()


COMMANDS = {
    "run": run_pipeline,
    "install": install,
    "uninstall": uninstall,
    "status": status,
}


if __name__ == "__main__":

    command = sys.argv[1] if len(sys.argv) > 1 else "run"

    if command not in COMMANDS:
        print(f"Unknown command : {command}")
        print(f"Expected one of : {', '.join(COMMANDS)}")
        sys.exit(2)

    sys.exit(COMMANDS[command]())
