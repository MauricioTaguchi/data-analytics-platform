"""Exercise maintenance failure recovery without Docker or live services."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

WRAPPER = Path(__file__).resolve().parents[2] / "deploy/backup/run-backup.sh"


@pytest.fixture
def maintenance(tmp_path):
    commands = tmp_path / "commands"
    commands.mkdir()
    docker = commands / "docker"
    docker.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if "ps" in args:
    print("api\\nworker\\nscheduler")
if "check-idle" in args and os.environ.get("FAIL_AT") == "idle":
    sys.exit(1)
if "stop" in args and os.environ.get("FAIL_AT") == "stop":
    sys.exit(1)
if "create" in args and os.environ.get("FAIL_AT") == "capture":
    sys.exit(1)
if "upload" in args and os.environ.get("FAIL_AT") == "upload":
    sys.exit(1)
if "create" in args:
    print('{"status": "captured"}')
''', encoding="utf-8")
    docker.chmod(0o700)
    # Do not chown real host directories merely to run a unit test.
    stat = commands / "stat"
    stat.write_text("#!/bin/sh\nif [ \"$2\" = '%a' ]; then echo 600; else echo 10001:700; fi\n", encoding="utf-8")
    stat.chmod(0o700)
    backups = tmp_path / "backups"
    backups.mkdir()
    configuration = tmp_path / "backup.env"
    configuration.write_text("# Protected operator configuration\n", encoding="utf-8")
    configuration.chmod(0o600)
    log = tmp_path / "commands.jsonl"
    environment = {**os.environ, "PATH": f"{commands}:{os.environ['PATH']}",
                   "DATAFLOW_PROJECT_DIR": str(tmp_path), "DATAFLOW_BACKUP_DIR": str(backups),
                   "DATAFLOW_BACKUP_ENV_FILE": str(configuration), "DATAFLOW_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
                   "COMMAND_LOG": str(log), "DATAFLOW_BACKUP_S3_BUCKET": "test-private-backups"}

    def run(fail_at=""):
        result = subprocess.run(["bash", str(WRAPPER)], env={**environment, "FAIL_AT": fail_at},
                                capture_output=True, text=True, timeout=10)
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls
    return run


def test_busy_jobs_do_not_stop_any_service(maintenance):
    result, calls = maintenance("idle")
    assert result.returncode != 0
    assert not any("stop" in call or "start" in call for call in calls)


@pytest.mark.parametrize("failure", ["stop", "capture"])
def test_failed_backup_restarts_exactly_the_previously_running_services(maintenance, failure):
    result, calls = maintenance(failure)
    assert result.returncode != 0
    starts = [call[call.index("start") + 1:] for call in calls if "start" in call]
    assert starts == [["api", "worker", "scheduler"]]
    assert not any("upload" in call for call in calls)


def test_success_restarts_before_remote_upload_and_does_not_delete(maintenance):
    result, calls = maintenance()
    assert result.returncode == 0, result.stderr
    start = next(index for index, call in enumerate(calls) if "start" in call)
    upload = next(index for index, call in enumerate(calls) if "upload" in call)
    assert start < upload
    assert not any("down" in call or "rm" in call or "restore" in call for call in calls)


def test_failed_remote_upload_leaves_services_restarted(maintenance):
    result, calls = maintenance("upload")
    assert result.returncode != 0
    assert len([call for call in calls if "start" in call]) == 1
